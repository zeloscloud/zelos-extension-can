"""CANopen protocol handler for CAN extension.

Provides NMT state tracking, heartbeat monitoring, EMERGENCY logging,
PDO decoding (with optional EDS), and passive SDO observation.
"""

import logging
from collections import deque
from typing import Any

import can
import zelos_sdk

from ..base import ProtocolHandler
from .pdo import PDODecoder
from .protocol import (
    PDO_FUNCTION_CODES,
    FunctionCode,
    NMTMonitor,
    decode_emergency,
    parse_cob_id,
)
from .sdo import SDOObserver

logger = logging.getLogger(__name__)


class CANopenHandler(ProtocolHandler):
    """CANopen protocol handler with NMT, HEARTBEAT, EMERGENCY, PDO, and SDO support."""

    def __init__(
        self,
        config: dict,
        source: zelos_sdk.TraceSource,
        namespace: zelos_sdk.TraceNamespace | None,
        bus_name: str | None,
    ) -> None:
        super().__init__(config, source, namespace, bus_name)

        canopen_opts = config.get("canopen_options", {})
        self._track_heartbeats = canopen_opts.get("track_heartbeats", True)
        self._track_emergencies = canopen_opts.get("track_emergencies", True)
        self._observe_sdo = canopen_opts.get("observe_sdo", False)

        # NMT monitor
        self._nmt = NMTMonitor()

        # EMERGENCY log (bounded)
        self._emergencies: deque[dict[str, Any]] = deque(maxlen=100)

        # SDO observer (optional)
        self._sdo_observer = SDOObserver() if self._observe_sdo else None

        # PDO decoder
        self._pdo_decoder = PDODecoder()

        # Try to load EDS if configured
        eds_file = canopen_opts.get("eds_file", "")
        node_ids_str = canopen_opts.get("node_ids", "")
        if eds_file:
            self._load_eds(eds_file, node_ids_str)

        # Create trace events
        source_prefix = f"{bus_name}_canopen" if bus_name else "canopen"
        self._canopen_source = self._create_trace_source(source_prefix)

        self._heartbeat_event = self._canopen_source.add_event(
            "heartbeat",
            [
                zelos_sdk.TraceEventFieldMetadata(
                    name="node_id", data_type=zelos_sdk.DataType.UInt8, unit=None
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    name="state", data_type=zelos_sdk.DataType.String, unit=None
                ),
            ],
        )

        self._nmt_cmd_event = self._canopen_source.add_event(
            "nmt_command",
            [
                zelos_sdk.TraceEventFieldMetadata(
                    name="command", data_type=zelos_sdk.DataType.UInt8, unit=None
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    name="target_node", data_type=zelos_sdk.DataType.UInt8, unit=None
                ),
            ],
        )

        self._emergency_event = self._canopen_source.add_event(
            "emergency",
            [
                zelos_sdk.TraceEventFieldMetadata(
                    name="node_id", data_type=zelos_sdk.DataType.UInt8, unit=None
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    name="error_code", data_type=zelos_sdk.DataType.UInt16, unit=None
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    name="error_register", data_type=zelos_sdk.DataType.UInt8, unit=None
                ),
            ],
        )

        # PDO events are created lazily as PDOs are encountered
        self._pdo_events: dict[int, Any] = {}

        logger.info("CANopen handler initialized")

    def handle_frame(self, msg: can.Message, timestamp_ns: int | None) -> bool:
        """Process a CAN frame for CANopen protocol handling."""
        if msg.is_extended_id:
            return False  # CANopen uses 11-bit IDs only

        func, node_id = parse_cob_id(msg.arbitration_id)
        if func is None:
            return False

        if func == FunctionCode.HEARTBEAT:
            if self._track_heartbeats:
                state_name = self._nmt.handle_heartbeat(node_id, msg.data)
                if state_name is not None:
                    self._log_event(
                        self._heartbeat_event,
                        timestamp_ns,
                        node_id=node_id,
                        state=state_name,
                    )
            return True

        if func == FunctionCode.EMERGENCY:
            if self._track_emergencies:
                self._emit_emergency_event(node_id, msg.data, timestamp_ns)
            return True

        if func == FunctionCode.NMT:
            result = self._nmt.handle_nmt_command(msg.data)
            if result:
                self._log_event(
                    self._nmt_cmd_event,
                    timestamp_ns,
                    command=result[0],
                    target_node=result[1],
                )
            return True

        if func == FunctionCode.SYNC:
            return True

        if func in (FunctionCode.SDO_TX, FunctionCode.SDO_RX) and self._sdo_observer:
            self._sdo_observer.handle(node_id, func == FunctionCode.SDO_TX, msg.data)
            return True

        if func in PDO_FUNCTION_CODES:
            return self._handle_pdo(func, node_id, msg, timestamp_ns)

        return False

    def get_status(self) -> dict[str, Any]:
        return {
            "protocol": "canopen",
            "known_nodes": self._nmt.get_node_count(),
            "emergency_count": len(self._emergencies),
        }

    def get_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "known_nodes": self._nmt.get_node_count(),
            "emergency_count": len(self._emergencies),
            "pdo_mappings": self._pdo_decoder.mapping_count,
        }
        if self._sdo_observer:
            metrics["sdo_transfers"] = self._sdo_observer.total_transfers
            metrics["sdo_aborted"] = self._sdo_observer.aborted_transfers
        return metrics

    def cleanup(self) -> None:
        """Clean stale SDO pending requests."""
        if self._sdo_observer:
            self._sdo_observer.cleanup_stale()

    def get_node_states(self) -> dict[str, Any]:
        """Get NMT states of all known nodes."""
        states = self._nmt.get_node_states()
        # Convert int keys to strings for JSON serialization
        return {"count": len(states), "nodes": {str(k): v for k, v in states.items()}}

    def get_emergencies(self) -> dict[str, Any]:
        """Get recent EMERGENCY messages."""
        return {"count": len(self._emergencies), "emergencies": list(self._emergencies)[-20:]}

    def get_sdo_transfers(self) -> dict[str, Any]:
        """Get recent SDO transfers."""
        if not self._sdo_observer:
            return {"enabled": False, "message": "SDO observation is disabled"}
        transfers = self._sdo_observer.get_recent_transfers()
        return {
            "enabled": True,
            "total": self._sdo_observer.total_transfers,
            "aborted": self._sdo_observer.aborted_transfers,
            "recent": transfers,
        }

    def get_pdo_mappings(self) -> dict[str, Any]:
        """Get configured PDO mappings."""
        mappings = {}
        for cob_id, pdo_maps in self._pdo_decoder.get_all_mappings().items():
            mappings[f"0x{cob_id:03X}"] = [{"name": m.name, "bits": m.bit_length} for m in pdo_maps]
        return {"count": len(mappings), "mappings": mappings}

    def _load_eds(self, eds_file: str, node_ids_str: str) -> None:
        """Load EDS file and build PDO decoder."""
        try:
            from .eds import build_pdo_decoder_from_eds

            node_ids = []
            if node_ids_str:
                for part in node_ids_str.split(","):
                    part = part.strip()
                    if part:
                        node_ids.append(int(part))

            if not node_ids:
                node_ids = [1]  # Default to node 1

            for node_id in node_ids:
                decoder = build_pdo_decoder_from_eds(eds_file, node_id)
                for cob_id, mappings in decoder.get_all_mappings().items():
                    self._pdo_decoder.add_mapping(cob_id, mappings)

            logger.info("Loaded EDS file '%s' for nodes %s", eds_file, node_ids)
        except Exception as e:
            logger.error("Failed to load EDS file '%s': %s", eds_file, e)

    def _handle_pdo(
        self,
        func: FunctionCode,
        node_id: int,
        msg: can.Message,
        timestamp_ns: int | None,
    ) -> bool:
        """Handle a PDO frame. Decodes if EDS mapping available."""
        cob_id = msg.arbitration_id

        if self._pdo_decoder.has_mapping(cob_id):
            decoded = self._pdo_decoder.decode(cob_id, msg.data)
            if decoded:
                self._emit_pdo_event(func, node_id, cob_id, decoded, timestamp_ns)
                return True

        return False

    def _emit_emergency_event(self, node_id: int, data: bytes, timestamp_ns: int | None) -> None:
        """Emit EMERGENCY trace event and record in log."""
        emcy = decode_emergency(node_id, data)
        if not emcy:
            return

        self._emergencies.append(
            {
                "node_id": node_id,
                "error_code": f"0x{emcy.error_code:04X}",
                "error_register": f"0x{emcy.error_register:02X}",
            }
        )

        self._log_event(
            self._emergency_event,
            timestamp_ns,
            node_id=node_id,
            error_code=emcy.error_code,
            error_register=emcy.error_register,
        )

    def _emit_pdo_event(
        self,
        func: FunctionCode,
        node_id: int,
        cob_id: int,
        decoded: dict[str, Any],
        timestamp_ns: int | None,
    ) -> None:
        """Emit decoded PDO as a trace event."""
        event = self._pdo_events.get(cob_id)

        if event is None:
            event_name = f"{func.name}_node{node_id}"
            fields = [
                zelos_sdk.TraceEventFieldMetadata(
                    name=name,
                    data_type=self._infer_data_type(value),
                    unit=None,
                )
                for name, value in decoded.items()
            ]
            event = self._canopen_source.add_event(event_name, fields)
            self._pdo_events[cob_id] = event

        self._log_event(event, timestamp_ns, **decoded)

    @staticmethod
    def _infer_data_type(value: Any) -> zelos_sdk.DataType:
        """Infer trace data type from a Python value."""
        if isinstance(value, float):
            return zelos_sdk.DataType.Float32
        if isinstance(value, int):
            if value < 0:
                return zelos_sdk.DataType.Int32
            if value <= 255:
                return zelos_sdk.DataType.UInt8
            if value <= 65535:
                return zelos_sdk.DataType.UInt16
            return zelos_sdk.DataType.UInt32
        return zelos_sdk.DataType.String
