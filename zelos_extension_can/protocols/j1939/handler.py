"""J1939 protocol handler for CAN extension.

Provides PGN-aware metadata, transport protocol reassembly,
and DM1/DM2 diagnostic decoding alongside standard DBC signal decoding.
"""

import json
import logging
from typing import Any

import can
import zelos_sdk

from ..base import ProtocolHandler
from .diagnostics import decode_dm1
from .pgn import (
    PGN_DM1,
    PGN_DM2,
    build_arb_id,
    destination_from_frame_id,
    is_transport_frame,
    parse_frame_id,
    pgn_from_frame_id,
)
from .transport import TPStateMachine

logger = logging.getLogger(__name__)


class J1939Handler(ProtocolHandler):
    """J1939 protocol handler with PGN metadata, TP reassembly, and diagnostics."""

    def __init__(
        self,
        config: dict,
        source: zelos_sdk.TraceSource,
        namespace: zelos_sdk.TraceNamespace | None,
        bus_name: str | None,
    ) -> None:
        super().__init__(config, source, namespace, bus_name)

        self._track_addresses = config.get("track_source_addresses", True)
        self._decode_diagnostics = config.get("decode_diagnostics", True)
        tp_timeout = config.get("tp_timeout_ms", 1250)

        # Transport protocol state machine
        self._tp = TPStateMachine(
            on_complete=self._on_tp_complete,
            timeout_ms=tp_timeout,
        )

        # Address tracking: SA -> {pgn_count, last_seen_ns, pgns_seen}
        self._address_table: dict[int, dict[str, Any]] = {}

        # Diagnostics state
        self._active_dtcs: dict[int, dict] = {}  # SA -> latest DM1 info

        # Create trace events
        source_prefix = f"{bus_name}_j1939" if bus_name else "j1939"
        self._j1939_source = self._create_trace_source(source_prefix)

        self._pgn_meta_event = self._j1939_source.add_event(
            "pgn_meta",
            [
                zelos_sdk.TraceEventFieldMetadata(
                    name="pgn", data_type=zelos_sdk.DataType.UInt32, unit=None
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    name="source_address", data_type=zelos_sdk.DataType.UInt8, unit=None
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    name="priority", data_type=zelos_sdk.DataType.UInt8, unit=None
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    name="destination", data_type=zelos_sdk.DataType.UInt8, unit=None
                ),
            ],
        )

        self._tp_complete_event = self._j1939_source.add_event(
            "tp_complete",
            [
                zelos_sdk.TraceEventFieldMetadata(
                    name="pgn", data_type=zelos_sdk.DataType.UInt32, unit=None
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    name="source_address", data_type=zelos_sdk.DataType.UInt8, unit=None
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    name="total_bytes", data_type=zelos_sdk.DataType.UInt16, unit=None
                ),
                zelos_sdk.TraceEventFieldMetadata(
                    name="tp_type", data_type=zelos_sdk.DataType.String, unit=None
                ),
            ],
        )

        if self._decode_diagnostics:
            self._dm1_event = self._j1939_source.add_event(
                "dm1",
                [
                    zelos_sdk.TraceEventFieldMetadata(
                        name="source_address", data_type=zelos_sdk.DataType.UInt8, unit=None
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="lamp_status", data_type=zelos_sdk.DataType.UInt16, unit=None
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="dtc_count", data_type=zelos_sdk.DataType.UInt8, unit=None
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="dtcs", data_type=zelos_sdk.DataType.String, unit=None
                    ),
                ],
            )

        logger.info("J1939 handler initialized (TP timeout=%dms)", tp_timeout)

    def handle_frame(self, msg: can.Message, timestamp_ns: int | None) -> bool:
        """Process a CAN frame for J1939 protocol handling.

        :param msg: CAN message
        :param timestamp_ns: Timestamp in nanoseconds
        :return: True if consumed (TP frames), False for single-frame J1939 (DBC still decodes)
        """
        if not msg.is_extended_id:
            return False  # Not J1939, let DBC path handle it

        # Single unpack — derive PGN and destination from the already-unpacked frame_id
        frame_id = parse_frame_id(msg.arbitration_id)
        pgn = pgn_from_frame_id(frame_id)

        # Track source address
        if self._track_addresses:
            self._update_address_table(frame_id.source_address, pgn, timestamp_ns)

        # TP frames → state machine (consumed)
        if is_transport_frame(pgn):
            self._tp.handle_frame(msg, frame_id)
            return True  # Consumed — TP reassembly handles decode on completion

        # Single-frame J1939 → emit PGN metadata, then let DBC path decode signals
        destination = destination_from_frame_id(frame_id)
        self._log_event(
            self._pgn_meta_event,
            timestamp_ns,
            pgn=pgn,
            source_address=frame_id.source_address,
            priority=frame_id.priority,
            destination=destination,
        )

        # Check for single-frame DM1 (rare, but possible with <=1 DTC)
        if self._decode_diagnostics and pgn in (PGN_DM1, PGN_DM2):
            self._handle_dm1(frame_id.source_address, msg.data, timestamp_ns)

        return False  # NOT consumed — DBC path still decodes the signals

    def get_status(self) -> dict[str, Any]:
        return {
            "protocol": "j1939",
            "active_tp_sessions": self._tp.active_session_count,
            "tracked_addresses": len(self._address_table),
            "active_dtc_sources": len(self._active_dtcs),
        }

    def get_metrics(self) -> dict[str, Any]:
        return {
            "tp_completed": self._tp.completed_transfers,
            "tp_aborted": self._tp.aborted_transfers,
            "tp_timed_out": self._tp.timed_out_transfers,
            "active_tp_sessions": self._tp.active_session_count,
            "tracked_addresses": len(self._address_table),
        }

    def cleanup(self) -> None:
        """Clean stale TP sessions."""
        cleaned = self._tp.cleanup_stale()
        if cleaned:
            logger.debug("Cleaned %d stale TP sessions", cleaned)

    def get_address_table(self) -> dict[str, Any]:
        """Get discovered J1939 source addresses and PGN counts."""
        entries = []
        for sa, info in sorted(self._address_table.items()):
            entries.append(
                {
                    "address": f"0x{sa:02X}",
                    "pgn_count": len(info["pgns_seen"]),
                    "last_seen_ms": info.get("last_seen_ms", 0),
                }
            )
        return {"count": len(entries), "addresses": entries}

    def get_tp_sessions(self) -> dict[str, Any]:
        """Get transport protocol session stats."""
        return {
            "active_sessions": self._tp.active_session_count,
            "completed": self._tp.completed_transfers,
            "aborted": self._tp.aborted_transfers,
            "timed_out": self._tp.timed_out_transfers,
        }

    def get_diagnostics(self) -> dict[str, Any]:
        """Get active DM1/DM2 diagnostic trouble codes."""
        entries = []
        for sa, info in sorted(self._active_dtcs.items()):
            entries.append(
                {
                    "source_address": f"0x{sa:02X}",
                    "lamp_status": info["lamp_status"],
                    "dtc_count": info["dtc_count"],
                    "dtcs": info["dtcs"],
                }
            )
        return {"count": len(entries), "sources": entries}

    def _update_address_table(self, sa: int, pgn: int, timestamp_ns: int | None) -> None:
        """Track source addresses and their PGN activity."""
        if sa not in self._address_table:
            self._address_table[sa] = {"last_seen_ms": 0, "pgns_seen": set()}

        entry = self._address_table[sa]
        entry["pgns_seen"].add(pgn)

        if timestamp_ns is not None:
            entry["last_seen_ms"] = timestamp_ns // 1_000_000

    def _on_tp_complete(self, pgn: int, source_address: int, data: bytes, tp_type: str) -> None:
        """Callback when TP reassembly completes."""
        self._log_event(
            self._tp_complete_event,
            None,
            pgn=pgn,
            source_address=source_address,
            total_bytes=len(data),
            tp_type=tp_type,
        )

        # Decode DM1/DM2 if applicable
        if self._decode_diagnostics and pgn in (PGN_DM1, PGN_DM2):
            self._handle_dm1(source_address, data, None)

        # Try to decode via DBC if codec is available
        if self._codec is not None:
            try:
                synth_msg = can.Message(
                    arbitration_id=build_arb_id(pgn, source_address),
                    data=data,
                    is_extended_id=True,
                )
                self._codec._decode_and_emit_message(synth_msg, None)
            except Exception as e:
                logger.debug("Failed to decode TP-reassembled PGN 0x%04X: %s", pgn, e)

    def _handle_dm1(self, source_address: int, data: bytes, timestamp_ns: int | None) -> None:
        """Decode and emit DM1/DM2 diagnostic data."""
        lamp_status, dtcs = decode_dm1(data)

        dtc_dicts = [{"spn": d.spn, "fmi": d.fmi, "occ": d.occurrence} for d in dtcs]

        self._active_dtcs[source_address] = {
            "lamp_status": lamp_status,
            "dtc_count": len(dtcs),
            "dtcs": dtc_dicts,
        }

        self._log_event(
            self._dm1_event,
            timestamp_ns,
            source_address=source_address,
            lamp_status=lamp_status,
            dtc_count=len(dtcs),
            dtcs=json.dumps(dtc_dicts),
        )
