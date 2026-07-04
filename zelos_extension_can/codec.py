"""CAN bus codec with database decoding and transmission."""

import asyncio
import hashlib
import json
import logging
import math
import sys
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

import can
import cantools
import zelos_sdk

from .demo.demo import run_demo_ev_simulation
from .utils.schema_utils import cantools_signal_to_trace_metadata

logger = logging.getLogger(__name__)


# ─── Action-input parsers (module-level so tests hit them at the helper seam) ──


def _parse_can_id(can_id: str) -> int:
    """Accept `0x100`, `100`, or hex without prefix; always parse as hex."""
    return int(can_id.strip(), 16)


def _parse_data_hex(data: str) -> bytes:
    return bytes.fromhex(data.replace(" ", "").replace(",", ""))


def _validate_id_range(can_id: int, is_extended: bool) -> None:
    max_id = 0x1FFFFFFF if is_extended else 0x7FF
    if can_id < 0 or can_id > max_id:
        kind = "extended" if is_extended else "standard"
        raise ValueError(f"can_id 0x{can_id:x} out of range for {kind} ID (max 0x{max_id:x})")


def _task_id(can_id: int, is_extended: bool, mux: str = "raw") -> str:
    """Stable taskId within a single codec — arbitration ID + frame kind + discriminator.

    Starting a periodic with the same key replaces the existing slot and signals
    `replaced: True` to the caller. Matches the SocketCAN BCM kernel behavior
    (TX_SETUP on the same can_id replaces the existing slot).
    """
    ext = "ext" if is_extended else "std"
    return f"0x{can_id:x}:{ext}:{mux}"


def _parse_signals_json(raw: str) -> dict[str, Any]:
    if not raw.strip():
        raise ValueError("signals_json must be a JSON object string")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"signals_json is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("signals_json must decode to a JSON object")
    return parsed


def _parse_mux(mux: str) -> int | str | None:
    s = mux.strip()
    if not s:
        return None
    try:
        return int(s, 0)
    except ValueError:
        return s


def _encode_dbc(
    dbc_msg: cantools.database.can.Message,
    signals: dict[str, Any],
    mux_value: int | str | None,
) -> bytes:
    # cantools encode_message picks the right mux variant when the multiplexer
    # signal is present in the input. If the caller passed a standalone `mux`
    # field, inject it under the multiplexer signal name.
    payload = dict(signals)
    if mux_value is not None and dbc_msg.is_multiplexed():
        mux_signal = next((sig for sig in dbc_msg.signals if sig.is_multiplexer), None)
        if mux_signal is not None and mux_signal.name not in payload:
            payload[mux_signal.name] = mux_value
    # strict=False lets authors send sentinel / SNA values that fall outside
    # the DBC's declared [min|max] but still fit the signal's bit field
    # (common pattern: raw 0xFF on an 8-bit field to mark "signal not
    # available"). The bit-field range itself is still enforced by cantools;
    # the webapp does an additional pre-flight check against the bit-field
    # range so out-of-bits values are caught before they reach us.
    return bytes(dbc_msg.encode(payload, strict=False))


def _describe_dbc_message_summary(msg: cantools.database.can.Message) -> dict[str, Any]:
    """Lightweight identifier-only shape returned by list_messages. Drops the
    signal array so the catalog fetch stays cheap even on multi-thousand-
    message DBCs. The webapp fetches per-message detail via describe_message
    when a specific message is picked."""
    return {
        "name": msg.name,
        "can_id": int(msg.frame_id),
        "is_extended": bool(msg.is_extended_frame),
        "dlc": int(msg.length),
        "cycle_time_ms": msg.cycle_time,
    }


def _describe_dbc_message(msg: cantools.database.can.Message) -> dict[str, Any]:
    return {
        **_describe_dbc_message_summary(msg),
        "signals": [_describe_dbc_signal(sig) for sig in msg.signals],
    }


def _hash_dbc_file(path: str | Path) -> str:
    """Cache-busting fingerprint for a DBC file. The webapp keys its
    list_messages React Query by this so any post-reload change forces a
    re-fetch. SHA1 truncated to 16 hex chars — collision risk is irrelevant
    here, the field is purely a same-vs-different signal."""
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:16]


def _derive_bus_status(running: bool, bus: Any) -> str:
    """Map (running, python-can BusState) to one of the four wire-contract
    statuses the app expects: active / stopped / error / unknown.

    Virtual / fake / file backends often raise on `bus.state` or don't
    return a real `can.BusState` enum; on those we trust `running` and
    fall back to "active"."""
    if not running or bus is None:
        return "stopped"
    try:
        state = bus.state
    except Exception:
        return "active"
    if not isinstance(state, can.BusState):
        return "active"
    if state == can.BusState.ACTIVE:
        return "active"
    if state in {can.BusState.ERROR, can.BusState.PASSIVE}:
        return "error"
    return "unknown"


def _describe_dbc_signal(sig: cantools.database.can.Signal) -> dict[str, Any]:
    # JSON requires string dict keys, and the wire encoder rejects Decimal —
    # coerce scale/offset/min/max to float and value_table keys to str.
    scale = float(sig.scale) if sig.scale is not None else 1.0
    offset = float(sig.offset) if sig.offset is not None else 0.0
    return {
        "name": sig.name,
        "start_bit": int(sig.start),
        "length": int(sig.length),
        "byte_order": "little" if sig.byte_order == "little_endian" else "big",
        "is_signed": bool(sig.is_signed),
        "scale": scale,
        "offset": offset,
        "min": float(sig.minimum) if sig.minimum is not None else None,
        "max": float(sig.maximum) if sig.maximum is not None else None,
        "unit": sig.unit,
        "value_table": _physical_value_table(sig, scale, offset),
        "mux_indicator": bool(sig.is_multiplexer),
        "mux_value": int(sig.multiplexer_ids[0]) if sig.multiplexer_ids else None,
    }


def _scale_precision(scale: float) -> int:
    """Decimal places implied by a signal's scale. scale=0.001 → 3,
    scale=0.1 → 1, scale=1 → 0, scale=10 → 0 (no fractional precision).
    Used to trim fp64 noise out of decoded physical values so they
    string-match the value_table keys produced by `_physical_value_table`
    and so the trace shows the same precision the wire actually carries."""
    if not scale or scale <= 0 or scale >= 1:
        return 0
    return max(0, -math.floor(math.log10(scale)))


def _physical_value_table(
    sig: cantools.database.can.Signal, scale: float, offset: float
) -> dict[str, str] | None:
    """JSON-wire form of the physical value table, used by describe_message.

    See `_value_table_for_trace` for the in-process float/int dict form used
    by zelos-sdk's `add_value_table`. Both must agree on the physical key so
    a value emitted to the trace matches the value-table entry exactly."""
    numeric = _value_table_for_trace(sig)
    if numeric is None:
        return None
    return {format(k, ".10g") if isinstance(k, float) else str(k): v for k, v in numeric.items()}


def _value_table_for_trace(
    sig: cantools.database.can.Signal,
) -> dict[int | float, str] | None:
    """Build a value table keyed on the physical (scaled+offset) value, so
    trace consumers' lookups match the values we actually emit.

    DBC `VAL_` entries map RAW integer values to labels by convention. For
    enum signals (scale=1, offset=0) the raw int IS the physical value, so
    we use int keys. For scaled signals (e.g. cell_voltage with scale 0.001)
    the physical value is float; we convert and round to the scale's
    precision so the key matches the value `_convert_signals` will emit
    (which is also `round(decoded, precision)`)."""
    if not sig.choices:
        return None
    scale = float(sig.scale) if sig.scale is not None else 1.0
    offset = float(sig.offset) if sig.offset is not None else 0.0
    precision = _scale_precision(scale)
    out: dict[int | float, str] = {}
    for raw_int, label in sig.choices.items():
        if scale == 1.0 and offset == 0.0:
            out[int(raw_int)] = str(label)
        else:
            physical = int(raw_int) * scale + offset
            key = round(physical, precision) if precision > 0 else physical
            out[key] = str(label)
    return out


@dataclass(slots=True)
class Metrics:
    """Performance metrics for CAN codec operations."""

    messages_received: int = 0
    messages_decoded: int = 0
    decode_errors: int = 0
    unknown_messages: int = 0
    # Counts CanError raised by the synchronous bus.send() in one-shot
    # send_raw / send_message paths. Periodics go through python-can's
    # CyclicSendTask which runs its own thread and swallows errors
    # internally — tracking those is out of scope until we wrap the task.
    tx_errors: int = 0
    # Reserved for future BCM queue-overflow tracking; currently always 0.
    # The shape is kept stable so the app's wire contract doesn't churn.
    tx_overflows: int = 0


class TimestampMode(IntEnum):
    """Timestamp handling modes for efficient comparison."""

    IGNORE = 0
    ABSOLUTE = 1
    AUTO = 2


class CanCodec(can.Listener):
    """CAN bus monitor with database decoding and periodic transmission support."""

    def __init__(
        self,
        config: dict[str, Any],
        namespace: zelos_sdk.TraceNamespace | None = None,
        bus_name: str | None = None,
    ) -> None:
        """Initialize CAN codec.

        :param config: Configuration dictionary with interface, channel, database_file
        :param namespace: Optional isolated TraceNamespace for the TraceSource
        :param bus_name: Optional name prefix for trace sources (for multi-bus setups)
        """
        self.config = config
        self.namespace = namespace
        self.bus_name = bus_name
        self.running = False
        self.last_message_time = time.time()
        self.start_time = time.time()

        # zelos-socketcan and ssh-socketcan both run the full recv -> DBC decode
        # -> trace pipeline in Rust (zelos_can.CanCodec): no python-can Notifier,
        # no cantools, and no per-frame Python on RX. self._native holds that
        # codec while running. TX actions still go through self.bus (a python-can
        # compat bus for zelos-socketcan, or a CodecTxAdapter over the Rust codec
        # for ssh-socketcan) so the bus-based action layer below is reused as-is.
        #
        #   - zelos-socketcan: Rust owns a real SocketCAN socket (Linux-only) and
        #     self-heals its recv loop internally.
        #   - ssh-socketcan: Rust decodes frames shuttled over ssh by a disposable
        #     SshTransport feeding a durable zelos_can.ExternalBus (any OS); the
        #     Python health supervisor rebuilds the transport on failure.
        #
        # self._use_rust unifies the "Rust owns RX/decode/schema/metrics" seams so
        # the native path stays byte-identical while ssh shares them.
        self._use_native = config.get("interface") == "zelos-socketcan"
        self._use_ssh = config.get("interface") == "ssh-socketcan"
        self._use_rust = self._use_native or self._use_ssh
        self._native: Any = None
        # RX and TX counter snapshots taken at stop(), before the Rust handle is
        # dropped, so get_tx_state keeps reporting the final values afterward.
        self._native_metrics: dict[str, int] | None = None
        self._native_tx_metrics: dict[str, int] | None = None
        # ssh-socketcan only: the durable ExternalBus and the disposable
        # SshTransport (rebuilt on reconnect). None on every other interface.
        self._ebus: Any = None
        self._transport: Any = None

        # Timestamp handling - use enum for fast comparison
        timestamp_mode_str = config.get("timestamp_mode", "auto").upper()
        self.timestamp_mode = TimestampMode[timestamp_mode_str]
        self.hw_timestamp_offset: float | None = None  # Offset to convert HW time to wall-clock
        self.first_hw_timestamp: float | None = None  # First HW timestamp seen

        # Cache frequently accessed config values as booleans to avoid repeated string hashing
        self.log_raw_frames = config.get("log_raw_frames", False)
        self.fd_mode = config.get("fd_mode", False)
        self.emit_schemas_on_init = config.get("emit_schemas_on_init", False)

        # Metrics tracking
        self.metrics = Metrics()

        # Demo mode simulation
        self.demo_mode = config.get("demo_mode", False)
        self.demo_task: asyncio.Task | None = None

        # Load and validate database file
        database_path = config["database_file"]

        if not Path(database_path).exists():
            raise FileNotFoundError(f"CAN database file not found: {database_path}")

        # Store the resolved database file path for reuse in actions
        self.database_file_path = database_path

        logger.info("Loading CAN database file: %s", database_path)
        try:
            self.db = cantools.database.load_file(database_path)
            logger.info("Loaded %d messages from database", len(self.db.messages))
        except Exception as e:
            raise ValueError(f"Failed to load database file: {e}") from e

        # SHA1 of the file bytes, truncated for wire compactness. The webapp
        # uses this as a cache key for list_messages — any change to the file
        # (after a reload/restart) flips the hash and forces a re-fetch.
        self.dbc_hash = _hash_dbc_file(database_path)

        # Determine trace source name (use exact bus_name for multi-bus)
        source_name = self.bus_name if self.bus_name else "can_codec"
        raw_source_name = f"{self.bus_name}_raw" if self.bus_name else "can_raw"

        # Create trace source (in isolated namespace if provided)
        if self.namespace:
            self.source = zelos_sdk.TraceSource(source_name, namespace=self.namespace)
        else:
            self.source = zelos_sdk.TraceSource(source_name)

        # Create raw CAN frame event schema (for log_raw_frames feature)
        if self.log_raw_frames:
            if self.namespace:
                self.raw_source = zelos_sdk.TraceSource(raw_source_name, namespace=self.namespace)
            else:
                self.raw_source = zelos_sdk.TraceSource(raw_source_name)

            # On the Rust paths (zelos-socketcan / ssh-socketcan) the Rust codec
            # owns the raw-frame schema and emit; create the TraceSource (so it
            # lands in the right namespace and is handed to the codec) but don't
            # register an event here.
            self.raw_event = (
                None
                if self._use_rust
                else self.raw_source.add_event(
                    "messages",
                    [
                        zelos_sdk.TraceEventFieldMetadata(
                            name="arbitration_id", data_type=zelos_sdk.DataType.UInt32, unit=None
                        ),
                        zelos_sdk.TraceEventFieldMetadata(
                            name="dlc", data_type=zelos_sdk.DataType.UInt8, unit=None
                        ),
                        zelos_sdk.TraceEventFieldMetadata(
                            name="data", data_type=zelos_sdk.DataType.Binary, unit=None
                        ),
                    ],
                )
            )
        else:
            self.raw_source = None
            self.raw_event = None

        # Build message lookup tables (handle duplicates permissively)
        self.messages_by_id: dict[tuple[int, bool], cantools.database.can.Message] = {}
        self.messages_by_name: dict[str, cantools.database.can.Message] = {}

        self._events: dict[tuple[int, bool] | tuple[int, bool, int], Any] = {}

        for msg in self.db.messages:
            self.messages_by_id[self._message_key(msg.frame_id, msg.is_extended_frame)] = msg
            # Only store first occurrence of duplicate names
            if msg.name not in self.messages_by_name:
                self.messages_by_name[msg.name] = msg
            else:
                logger.warning(
                    f"Duplicate message name '{msg.name}' (ID {msg.frame_id}), "
                    "access via message ID instead"
                )

        # On the Rust paths (zelos-socketcan / ssh-socketcan) the Rust codec
        # generates/emits schemas itself (gated by its own emit_schemas_on_init);
        # don't double-register here.
        if self.emit_schemas_on_init and not self._use_rust:
            self._generate_all_schemas()
            logger.info("Generated %d event schemas from database", len(self._events))
        else:
            logger.info(
                "Schema generation deferred - will emit schemas as messages are encountered"
            )

        # Log raw frame configuration
        if self.log_raw_frames:
            logger.info(f"Raw CAN frame logging is ENABLED - logging to '{raw_source_name}'")
        else:
            logger.info("Raw CAN frame logging is DISABLED")

        self.bus: Any = None
        # python-can's CyclicSendTask. Owns its own thread, exposes `.stop()`
        # and `.modify_data()`; we don't manage an asyncio loop here because
        # action dispatch happens in worker threads where `asyncio.create_task`
        # raises "no running event loop".
        self._periodic_tasks: dict[str, can.broadcastmanager.CyclicSendTaskABC] = {}
        # Slot metadata so get_tx_state can reconstruct what each task is
        # sending without poking the task object's internals.
        self._periodic_slots: dict[str, dict[str, Any]] = {}

    def _message_key(self, frame_id: int, is_extended: bool) -> tuple[int, bool]:
        """Build a stable message lookup key from CAN ID and frame format."""
        return (frame_id, is_extended)

    def _get_event_name(self, msg: cantools.database.can.Message) -> str:
        """Get event name for message (format: {frame_id:04x}_{name}).

        :param msg: cantools message
        :return: Event name string
        """
        width = 8 if msg.is_extended_frame else 4
        return f"{msg.frame_id:0{width}x}_{msg.name}"

    def get_timestamp(self, hw_timestamp: float | None) -> int | None:
        """Get timestamp in nanoseconds for logging, handling boot-relative timestamps.

        This method handles different timestamp modes:
        - AUTO: Detects boot-relative timestamps (starting near zero) and converts
                them to wall-clock time by tracking the offset between hardware
                time and system time at first message.
        - ABSOLUTE: Uses hardware timestamp as-is (assumes it's already wall-clock time)
        - IGNORE: Returns None to use system time

        :param hw_timestamp: Hardware timestamp in seconds (can be None)
        :return: Timestamp in nanoseconds, or None to use system time
        """
        if hw_timestamp is None or self.timestamp_mode == TimestampMode.IGNORE:
            return None

        if self.timestamp_mode == TimestampMode.ABSOLUTE:
            return int(hw_timestamp * 1e9)

        # Auto mode: detect timestamp type and calculate offset if needed
        if self.hw_timestamp_offset is None:
            self.first_hw_timestamp = hw_timestamp
            wall_clock_time = time.time()

            # If timestamp is within 15 seconds of current time, treat as absolute wall-clock
            # Otherwise treat as monotonic timestamp needing adjustment to current time
            time_diff = abs(wall_clock_time - hw_timestamp)

            if time_diff < 15.0:
                self.hw_timestamp_offset = 0.0
                logger.info(
                    "Detected absolute timestamps (first=%.3f s). Using hardware timestamps as-is.",
                    hw_timestamp,
                )
            else:
                # Hardware timestamp is monotonic but not aligned with wall-clock time
                # This could be: boot-relative (dongle timer starts at 0), or
                # fixed-offset (PCAN-style timer started at arbitrary past time)
                # Either way, apply constant offset to map to current wall-clock time
                self.hw_timestamp_offset = wall_clock_time - hw_timestamp
                logger.info(
                    "Detected monotonic timestamps with offset (first=%.3f s, offset=%.3f s). "
                    "Mapping to wall-clock time while preserving relative timing.",
                    hw_timestamp,
                    self.hw_timestamp_offset,
                )

        # Apply offset to map monotonic timestamps to wall-clock time
        # The offset is constant, so relative timing between messages is preserved
        wall_clock_timestamp = hw_timestamp + self.hw_timestamp_offset
        return int(wall_clock_timestamp * 1e9)

    # Extension timestamp modes -> zelos_can.CanCodec modes. "absolute" maps to
    # "hardware" (kernel SO_TIMESTAMPNS, wall-clock on SocketCAN).
    _NATIVE_TIMESTAMP_MODE = {"AUTO": "auto", "ABSOLUTE": "hardware", "IGNORE": "ignore"}

    def start(self) -> None:
        """Initialize CAN bus connection with retry logic."""
        bus_id = f"[{self.bus_name}] " if self.bus_name else ""
        logger.info(
            f"{bus_id}Starting CAN bus: interface={self.config['interface']}, "
            f"channel={self.config['channel']}"
        )

        if self.config["interface"] == "zelos-socketcan" and sys.platform != "linux":
            raise can.CanInterfaceNotImplementedError(
                "The 'zelos-socketcan' interface is Linux-only (it wraps the Rust "
                "zelos-can SocketCAN bus). Use 'socketcan' on Linux, or 'pcan'/"
                "'kvaser'/'vector' on macOS/Windows."
            )

        if self._use_native:
            self._start_native()
            return

        if self._use_ssh:
            self._start_ssh()
            return

        bus_config = {
            "interface": self.config["interface"],
            "channel": self.config["channel"],
        }

        # Pass through optional bus config parameters if specified
        if "receive_own_messages" in self.config:
            bus_config["receive_own_messages"] = self.config["receive_own_messages"]

        if "bitrate" in self.config:
            bus_config["bitrate"] = self.config["bitrate"]

        if self.fd_mode:
            bus_config["fd"] = True
            if "data_bitrate" in self.config:
                bus_config["data_bitrate"] = self.config["data_bitrate"]

        # Merge additional config_json (advanced interface-specific options)
        if "config_json" in self.config and self.config["config_json"]:
            try:
                additional_config = json.loads(self.config["config_json"])
                logger.info("Merging additional config: %s", list(additional_config.keys()))
                bus_config.update(additional_config)
            except json.JSONDecodeError as e:
                logger.error("Failed to parse config_json: %s", e)
                raise ValueError(f"Invalid config_json: {e}") from e

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.bus = can.Bus(**bus_config)
                self.running = True
                logger.info("CAN bus started successfully")
                return
            except can.CanError as e:
                if attempt == max_retries - 1:
                    logger.error("Failed to initialize CAN bus after %d attempts", max_retries)
                    raise
                logger.warning("Bus init failed (attempt %d/%d): %s", attempt + 1, max_retries, e)
                time.sleep(1)

    def _start_native(self) -> None:
        """Start the Rust-first pipeline for zelos-socketcan.

        zelos_can.CanCodec owns recv -> DBC decode -> trace entirely in Rust
        (no python-can Notifier, no cantools, no per-frame Python) and begins
        receiving on construction. A python-can compat bus is opened on the
        same channel purely for the TX action layer; it is never polled for RX,
        and TX frames are still traced because the Rust RX socket receives them
        via local loopback.
        """
        import zelos_can

        source_name = self.bus_name if self.bus_name else "can_codec"
        kwargs: dict[str, Any] = {
            "database_file": self.database_file_path,
            "source_name": source_name,
            "source": self.source,
            "channel": self.config["channel"],
            "log_raw_frames": self.log_raw_frames,
            "emit_schemas_on_init": self.emit_schemas_on_init,
            "timestamp_mode": self._NATIVE_TIMESTAMP_MODE.get(self.timestamp_mode.name, "auto"),
            "fd": self.fd_mode,
        }
        if self.log_raw_frames:
            kwargs["raw_source"] = self.raw_source
        if self.config.get("rcvbuf_size") is not None:
            kwargs["rcvbuf_size"] = self.config["rcvbuf_size"]
        self._native = zelos_can.CanCodec(**kwargs)

        # TX-only python-can compat bus on the same channel; no Notifier is
        # attached so it does no RX work. The bus-based TX action layer
        # (send_raw / send_message / periodics) reuses this unchanged.
        self.bus = can.Bus(
            interface="zelos-socketcan", channel=self.config["channel"], fd=self.fd_mode
        )
        self.running = True
        logger.info("zelos-socketcan native codec started on %s", self.config["channel"])

    def _start_ssh(self) -> None:
        """Start the Rust-first pipeline for ssh-socketcan.

        Bridges a remote edge device's SocketCAN bus over ``ssh`` using the
        edge's own ``can-utils`` — nothing is deployed on the edge, so this runs
        on Linux/macOS/Windows. ``zelos_can.CanCodec`` owns decode -> trace -> TX
        channel -> periodics -> metrics entirely in Rust, fed by a durable
        ``zelos_can.ExternalBus``; an :class:`SshTransport` shuttles raw frames
        across two ssh procs (``candump`` RX, ``cansend`` TX). The transport is
        disposable and is rebuilt on reconnect while the codec, ExternalBus, and
        armed periodics survive (see ``_reconnect_bus``).

        Mirrors ``_start_native`` but drives the codec through the ExternalBus
        seam instead of a SocketCAN socket: the ``channel``/``rcvbuf_size`` kwargs
        are SocketCAN-only and are replaced by ``bus=self._ebus`` (frame format
        flows per-frame through ``inject``). ``fd`` is passed through for parity
        with ``_start_native`` so the codec applies CAN-FD decode semantics.
        ``self._native`` is reused so metrics / get_tx_state / stop need no extra
        code. TX actions go through a ``CodecTxAdapter`` presenting the small
        python-can-shaped surface the action layer touches.
        """
        import zelos_can

        from .ssh_socketcan import CodecTxAdapter, SshTransport

        source_name = self.bus_name if self.bus_name else "can_codec"
        self._ebus = zelos_can.ExternalBus()
        kwargs: dict[str, Any] = {
            "database_file": self.database_file_path,
            "source_name": source_name,
            "source": self.source,
            "log_raw_frames": self.log_raw_frames,
            "emit_schemas_on_init": self.emit_schemas_on_init,
            "timestamp_mode": self._NATIVE_TIMESTAMP_MODE.get(self.timestamp_mode.name, "auto"),
            "fd": self.fd_mode,
            "bus": self._ebus,
        }
        if self.log_raw_frames:
            kwargs["raw_source"] = self.raw_source
        self._native = zelos_can.CanCodec(**kwargs)

        self._transport = SshTransport(
            self._ebus,
            self.config["channel"],
            ssh_port=self.config.get("ssh_port", 22),
            ssh_key_path=self.config.get("ssh_key_path"),
            ssh_extra_opts=self.config.get("ssh_extra_opts"),
            fd_mode=self.fd_mode,
        )
        self.bus = CodecTxAdapter(self._native, self._transport, self.config["channel"])
        self.running = True
        logger.info("ssh-socketcan codec started on %s", self.config["channel"])

    def stop(self) -> None:
        """Stop CAN bus and periodic tasks."""
        bus_id = f"[{self.bus_name}] " if self.bus_name else ""
        logger.info(f"{bus_id}Stopping CAN codec")
        self.running = False

        if self.demo_task:
            self.demo_task.cancel()
            self.demo_task = None

        for tid, task in list(self._periodic_tasks.items()):
            logger.info("Stopping periodic task: %s", tid)
            task.stop()
        self._periodic_tasks.clear()
        self._periodic_slots.clear()

        if self._native is not None:
            # Snapshot RX + TX counters before tearing down — the native handle
            # goes away and get_tx_state must keep reporting the final values.
            self._native_metrics = self._native_rx_counts()
            self._native_tx_metrics = self._native_tx_counts()
            self._native.stop()
            self._native = None

        if self.bus:
            self.bus.shutdown()
            self.bus = None

        # Best-effort drain so any frames still buffered in the TraceSource
        # batcher land in the trace before we go quiet. flush() may not exist
        # on older zelos-sdk; swallow that so a missing helper never blocks
        # shutdown.
        for src in (self.source, getattr(self, "raw_source", None)):
            if src is None:
                continue
            flush = getattr(src, "flush", None)
            if not callable(flush):
                continue
            try:
                flush()
            except Exception as e:
                logger.debug("%sTraceSource.flush() raised during stop: %s", bus_id, e)

    def run(self) -> None:
        """Run async message reception loop."""
        asyncio.run(self._run_async())

    def _check_bus_health(self) -> bool:
        """Check if CAN bus is healthy.

        :return: True if bus is operational
        """
        if not self.bus:
            logger.debug("Bus health check: bus is None")
            return False

        # For virtual/demo interfaces, just check if bus object exists
        if self.config.get("interface") == "virtual" or self.demo_mode:
            return True

        # For hardware interfaces, check bus state
        bus_state = self.bus.state
        is_active = bus_state == can.BusState.ACTIVE

        if not is_active:
            logger.info("Bus health check failed: state is %s, expected ACTIVE", bus_state.name)

        return is_active

    async def _reconnect_bus(self) -> bool:
        """Attempt to reconnect to CAN bus.

        :return: True if reconnection successful
        """
        logger.debug("Attempting bus reconnection...")

        # ssh-socketcan: the Rust codec, the ExternalBus, and armed periodics are
        # DURABLE — only the SshTransport is disposable. Reconnect rebuilds the
        # transport ONLY and must NEVER run the generic rebuild below: that path
        # would build a second ExternalBus + CanCodec against the SAME trace
        # source (double-emitting every frame), orphan the live transport, and
        # reset RX counters. The blocking teardown + Popen spawns run off the
        # event loop so a wedged transport can't stall other buses' supervisors.
        if self._use_ssh:
            return await asyncio.to_thread(self._rebuild_ssh_transport)

        try:
            if self.bus:
                logger.debug("Shutting down existing bus object...")
                self.bus.shutdown()
                self.bus = None

            logger.debug("Waiting 1 second before reinitializing bus...")
            await asyncio.sleep(1)

            logger.debug("Reinitializing bus...")
            self.start()
            return True
        except Exception as e:
            logger.error("Bus reconnection failed: %s", e)
            return False

    def _rebuild_ssh_transport(self) -> bool:
        """Rebuild ONLY the ssh transport; codec + ExternalBus + periodics survive.

        Runs off the event loop (via ``asyncio.to_thread``): the teardown joins
        two threads and waits on two procs (~8 s worst case) then spawns two fresh
        ssh procs. Returns True on a clean rebuild, False on any failure — on
        failure the codec, its ``self.bus`` object identity, the ExternalBus, and
        armed periodics are left untouched and the supervisor retries next tick.
        Never touches ``self._native`` / ``self._ebus`` / ``self.bus`` identity.
        """
        if not self.running:
            return False
        if self._native is None or self._ebus is None or self._transport is None:
            logger.error(
                "ssh reconnect: codec/port not initialized "
                "(native=%s ebus=%s transport=%s); cannot rebuild transport",
                self._native is not None,
                self._ebus is not None,
                self._transport is not None,
            )
            return False

        from .ssh_socketcan import SshTransport

        # Reap the dead procs/threads first (idempotent + total).
        try:
            self._transport.teardown()
        except Exception:
            logger.exception("ssh reconnect: transport teardown raised (continuing)")

        # stop() may have raced us during the blocking teardown — bail before we
        # resurrect a transport on a codec that is shutting down.
        if not self.running:
            return False

        try:
            # Discard stale periodic backlog queued while the link was down.
            self._ebus.drain_tx()
            self._transport = SshTransport(
                self._ebus,
                self.config["channel"],
                ssh_port=self.config.get("ssh_port", 22),
                ssh_key_path=self.config.get("ssh_key_path"),
                ssh_extra_opts=self.config.get("ssh_extra_opts"),
                fd_mode=self.fd_mode,
            )
            self.bus.transport = self._transport
            logger.info("ssh transport rebuilt, codec preserved")
            return True
        except Exception as e:
            logger.warning("ssh transport rebuild failed, retrying next tick: %s", e)
            return False

    def on_message_received(self, message: can.Message) -> None:
        """Handle CAN message directly from notifier (can.Listener interface).

        This direct callback approach is more efficient than AsyncBufferedReader
        as it eliminates buffering overhead and async context switching.

        :param message: Received CAN message
        """
        self._handle_message(message)
        self.last_message_time = time.time()

    def _check_notifier_health(self, notifier: can.Notifier) -> bool:
        """Check if notifier threads are alive.

        :param notifier: CAN notifier instance
        :return: True if at least one notifier thread is alive
        """
        try:
            if not hasattr(notifier, "_readers"):
                return False

            for reader in notifier._readers:
                if isinstance(reader, threading.Thread):
                    if reader.is_alive():
                        return True
                    logger.debug("Notifier thread '%s' is not alive", reader.name)

            logger.debug("No alive notifier threads found")
            return False
        except Exception as e:
            logger.error("Exception while checking notifier thread status: %s", e)
            return False

    def _log_reconnection_reason(self, notifier_alive: bool, bus_healthy: bool) -> None:
        """Log detailed reason for reconnection.

        :param notifier_alive: Whether notifier threads are alive
        :param bus_healthy: Whether bus health check passed
        """
        if not notifier_alive and not bus_healthy:
            logger.error("Reconnection triggered: Both notifier thread stopped AND bus unhealthy")
        elif not notifier_alive:
            logger.error("Reconnection triggered: Notifier thread stopped (bus was healthy)")
        else:
            logger.error("Reconnection triggered: Bus health check failed (notifier was alive)")

    async def _handle_reconnection(self, notifier: can.Notifier) -> can.Notifier:
        """Handle bus reconnection and notifier recreation.

        :param notifier: Current notifier instance (will be stopped)
        :return: New notifier instance if successful, otherwise the old one
        """
        logger.debug("Stopping notifier...")
        notifier.stop()

        if await self._reconnect_bus():
            new_notifier = can.Notifier(self.bus, [self])
            return new_notifier
        else:
            logger.error("Reconnection failed - bus remains uninitialized, will retry in 5 seconds")
            return notifier

    async def _run_async(self) -> None:
        """Main async loop - health monitoring and reconnection handling.

        Message reception happens via on_message_received() callback, not in this loop.
        This approach is more efficient than AsyncBufferedReader + asyncio.wait_for().
        """
        # Native path: zelos_can.CanCodec runs its own Rust recv/decode/trace
        # loop and auto-reconnects internally. No python-can Notifier, no health
        # supervisor, no per-frame Python — just idle until stopped.
        if self._use_native:
            logger.info("Starting CAN rx (native zelos-socketcan pipeline)")
            try:
                while self.running:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                logger.info("CAN reader cancelled")
            return

        # ssh-socketcan path: the Rust codec owns RX/decode/trace/metrics (fed by
        # the ExternalBus), so there is no python-can Notifier. Unlike the native
        # SocketCAN codec (which self-heals internally), the ssh procs live in
        # Python, so a lightweight 5 s supervisor watches transport health via the
        # adapter's bus.state and rebuilds only the transport on failure — codec,
        # ExternalBus, and armed periodics survive the rebuild.
        if self._use_ssh:
            logger.info("Starting CAN rx (ssh-socketcan pipeline)")
            # Capped backoff so a long edge outage doesn't spam thousands of
            # rebuild/log cycles: probe every 5 s when healthy; on a failed
            # rebuild grow the interval (5 s → cap 60 s), reset to 5 s on success.
            healthy_interval = 5.0
            max_interval = 60.0
            interval = healthy_interval
            try:
                while self.running:
                    await asyncio.sleep(interval)
                    if self._check_bus_health():
                        interval = healthy_interval
                        continue
                    # Surface WHY the link went unhealthy (unreachable / timed
                    # out / auth / candump died) from the transport's rx stderr
                    # tail, so a slow-connect or runtime drop is diagnosable
                    # rather than a bare "unhealthy". Read BEFORE reconnect so we
                    # capture the genuine failure, not teardown "Killed" noise.
                    reason = self._transport.stderr_tail() if self._transport is not None else ""
                    if reason:
                        logger.error(
                            "Reconnection triggered: ssh transport unhealthy (ssh: %s)", reason
                        )
                    else:
                        logger.error("Reconnection triggered: ssh transport unhealthy")
                    if await self._reconnect_bus():
                        interval = healthy_interval
                    else:
                        interval = min(interval * 2, max_interval)
            except asyncio.CancelledError:
                logger.info("CAN reader cancelled")
            except Exception as e:
                logger.exception("Error in ssh-socketcan supervision loop: %s", e)
            return

        if not self.bus:
            logger.error("Bus not initialized, call start() first")
            return

        notifier = can.Notifier(self.bus, [self])

        if self.demo_mode:
            self.demo_task = asyncio.create_task(run_demo_ev_simulation(self.bus, self.db, self))
            logger.info("Started EV simulation task for demo mode")

        try:
            logger.info("Starting CAN message rx loop")
            while self.running:
                await asyncio.sleep(5.0)

                notifier_alive = self._check_notifier_health(notifier)
                bus_healthy = self._check_bus_health()

                if not notifier_alive or not bus_healthy:
                    self._log_reconnection_reason(notifier_alive, bus_healthy)
                    notifier = await self._handle_reconnection(notifier)
        except asyncio.CancelledError:
            logger.info("CAN reader cancelled")
        except Exception as e:
            logger.exception("Error in CAN reception loop: %s", e)
        finally:
            notifier.stop()
            logger.info("CAN reception stopped")

    def _update_receive_metrics(self, msg: can.Message) -> None:
        """Update metrics for received message.

        :param msg: Received CAN message
        """
        self.metrics.messages_received += 1

    def _emit_raw_frame(self, msg: can.Message, timestamp_ns: int | None) -> None:
        """Emit raw CAN frame to trace if logging is enabled.

        :param msg: CAN message
        :param timestamp_ns: Timestamp in nanoseconds
        """
        if not self.log_raw_frames:
            return

        if timestamp_ns is None:
            self.raw_event.log(
                arbitration_id=msg.arbitration_id,
                dlc=msg.dlc,
                data=msg.data,
            )
        else:
            self.raw_event.log_at(
                timestamp_ns,
                arbitration_id=msg.arbitration_id,
                dlc=msg.dlc,
                data=msg.data,
            )

    def _decode_and_emit_message(self, msg: can.Message, timestamp_ns: int | None) -> None:
        """Decode CAN message and emit decoded signals to trace.

        :param msg: CAN message
        :param timestamp_ns: Timestamp in nanoseconds
        """
        try:
            key = self._message_key(msg.arbitration_id, msg.is_extended_id)
            dbc_msg = self.messages_by_id.get(key)
            if not dbc_msg:
                logger.debug(
                    "Unknown message ID: %04x (extended=%s)", msg.arbitration_id, msg.is_extended_id
                )
                self.metrics.unknown_messages += 1
                return

            # decode_choices=False so a value-table hit doesn't replace the
            # scaled physical value with a NamedSignalValue wrapper that
            # carries the raw int. The trace consistently sees the physical
            # value (e.g. 4.095 V for a raw 4095 / scale 0.001 SNA reading);
            # value-table label lookup is a UI concern, served by
            # describe_message's physical-keyed value_table.
            decoded = dbc_msg.decode(msg.data, decode_choices=False)
            self.metrics.messages_decoded += 1

            # Emit base signals (non-multiplexed signals + multiplexer signal if present)
            self._emit_base_signals(dbc_msg, decoded, timestamp_ns)
            if dbc_msg.is_multiplexed():
                self._emit_multiplexed_signals(dbc_msg, decoded, timestamp_ns)

        except KeyError:
            logger.debug("Message ID %04x not in database", msg.arbitration_id)
            self.metrics.unknown_messages += 1
        except cantools.database.DecodeError as e:
            logger.debug("Decode error for %04x: %s", msg.arbitration_id, e)
            self.metrics.decode_errors += 1
        except Exception as e:
            logger.debug("Error decoding message %04x: %s", msg.arbitration_id, e)
            self.metrics.decode_errors += 1

    def _handle_message(self, msg: can.Message) -> None:
        """Decode and emit CAN message to trace.

        For multiplexed messages, emits TWO separate events to minimize memory footprint:
        1. Base signals (including multiplexer): {id:04x}_{name}
        2. Multiplexed signals: {id:04x}_{name}/{mux_value}

        :param msg: Received CAN message
        """
        logger.debug("Received CAN message: %s", msg)
        self._update_receive_metrics(msg)
        timestamp_ns = self.get_timestamp(msg.timestamp)
        self._emit_raw_frame(msg, timestamp_ns)
        self._decode_and_emit_message(msg, timestamp_ns)

    def _generate_all_schemas(self) -> None:
        """Generate trace event schemas for all messages in database at init time.

        This provides visibility into what messages are defined, even before they're received.
        For multiplexed messages, generates schemas for all possible mux values.
        """
        for dbc_msg in self.db.messages:
            self._generate_base_schema(dbc_msg)

            if dbc_msg.is_multiplexed():
                self._generate_mux_schemas(dbc_msg)

    def _generate_base_schema(self, dbc_msg: cantools.database.can.Message) -> None:
        """Generate schema for base (non-multiplexed) signals.

        :param dbc_msg: DBC message definition
        """
        cache_key = self._message_key(dbc_msg.frame_id, dbc_msg.is_extended_frame)
        event_name = self._get_event_name(dbc_msg)
        base_signals = [sig for sig in dbc_msg.signals if not sig.multiplexer_ids]

        if base_signals:
            fields = [cantools_signal_to_trace_metadata(sig) for sig in base_signals]
            event = self.source.add_event(event_name, fields)

            for sig in base_signals:
                value_table = _value_table_for_trace(sig)
                if value_table:
                    self.source.add_value_table(event_name, sig.name, value_table)

            self._events[cache_key] = event
            logger.debug("Generated base schema: '%s' (%d signals)", event_name, len(fields))

    def _generate_mux_schemas(self, dbc_msg: cantools.database.can.Message) -> None:
        """Generate schemas for all multiplexed signal variants.

        :param dbc_msg: DBC message definition
        """
        mux_signal = next((sig for sig in dbc_msg.signals if sig.is_multiplexer), None)
        if not mux_signal:
            return

        # Collect all unique mux values from the signals
        mux_values: set[int] = set()
        for sig in dbc_msg.signals:
            if sig.multiplexer_ids:
                mux_values.update(sig.multiplexer_ids)

        for mux_value_int in sorted(mux_values):
            self._generate_mux_schema_for_value(dbc_msg, mux_value_int)

    def _generate_mux_schema_for_value(
        self, dbc_msg: cantools.database.can.Message, mux_value_int: int
    ) -> None:
        """Generate schema for a specific multiplexed signal variant.

        :param dbc_msg: DBC message definition
        :param mux_value_int: Multiplexer value to generate schema for
        """
        mux_signal = next((sig for sig in dbc_msg.signals if sig.is_multiplexer), None)
        if not mux_signal:
            return

        cache_key = (dbc_msg.frame_id, dbc_msg.is_extended_frame, mux_value_int)

        # Skip if already generated
        if cache_key in self._events:
            return

        # Use enum name if available, otherwise stringified integer
        if mux_signal.choices and mux_value_int in mux_signal.choices:
            mux_value_str = mux_signal.choices[mux_value_int]
        else:
            mux_value_str = str(mux_value_int)

        event_name = f"{self._get_event_name(dbc_msg)}/{mux_value_str}"
        mux_signals = [
            sig for sig in dbc_msg.signals if mux_value_int in (sig.multiplexer_ids or [])
        ]

        if mux_signals:
            fields = [cantools_signal_to_trace_metadata(sig) for sig in mux_signals]
            event = self.source.add_event(event_name, fields)

            for sig in mux_signals:
                value_table = _value_table_for_trace(sig)
                if value_table:
                    self.source.add_value_table(event_name, sig.name, value_table)

            self._events[cache_key] = event
            logger.debug("Generated mux schema: '%s' (%d signals)", event_name, len(fields))

    def _emit_signals(
        self,
        event: Any,
        signals: dict[str, int | float],
        timestamp_ns: int | None,
        context: str,
    ) -> None:
        """Emit trace event with error handling.

        :param event: Event to emit
        :param signals: Signal name->value mapping
        :param timestamp_ns: Timestamp in nanoseconds, or None
        :param context: Context string for logging (e.g., message name)
        """
        try:
            if timestamp_ns is not None:
                event.log_at(timestamp_ns, **signals)
            else:
                event.log(**signals)
            logger.debug("Emitted %s: %s", context, signals)
        except (OverflowError, ValueError) as e:
            logger.debug("Skipping emission for %s: %s", context, e)
            self.metrics.decode_errors += 1

    def _emit_base_signals(
        self, dbc_msg: cantools.database.can.Message, decoded: dict, timestamp_ns: int | None
    ) -> None:
        """Emit base (non-multiplexed) signals including multiplexer.

        :param dbc_msg: DBC message definition
        :param decoded: Decoded signal values
        :param timestamp_ns: Timestamp in nanoseconds, or None
        """
        cache_key = self._message_key(dbc_msg.frame_id, dbc_msg.is_extended_frame)
        event = self._events.get(cache_key)

        # Generate schema lazily if not already present
        if event is None and not self.emit_schemas_on_init:
            self._generate_base_schema(dbc_msg)
            event = self._events.get(cache_key)

        if event:
            signals = self._convert_signals(dbc_msg, decoded, base_only=True)
            self._emit_signals(event, signals, timestamp_ns, f"base:{dbc_msg.name}")

    def _emit_multiplexed_signals(
        self,
        dbc_msg: cantools.database.can.Message,
        decoded: dict,
        timestamp_ns: int | None,
    ) -> None:
        """Emit multiplexed signals for the active mux value.

        :param dbc_msg: DBC message definition
        :param decoded: Decoded signal values
        :param timestamp_ns: Timestamp in nanoseconds, or None
        """
        mux_signal = next((sig for sig in dbc_msg.signals if sig.is_multiplexer), None)
        if not mux_signal:
            return

        mux_value = decoded.get(mux_signal.name)
        if mux_value is None:
            return

        if isinstance(mux_value, int | float):
            mux_value_int = int(mux_value)
        else:
            # NamedSignalValue - get integer representation
            mux_value_int = int(mux_signal.conversion.choice_to_number(mux_value))

        cache_key = (dbc_msg.frame_id, dbc_msg.is_extended_frame, mux_value_int)
        event = self._events.get(cache_key)

        # Generate mux schema lazily if not already present
        if event is None and not self.emit_schemas_on_init:
            self._generate_mux_schema_for_value(dbc_msg, mux_value_int)
            event = self._events.get(cache_key)

        if event:
            # Get string representation for debug logging
            if isinstance(mux_value, int | float):
                mux_value_str = str(mux_value_int)
            else:
                mux_value_str = str(mux_value)

            signals = self._convert_signals(dbc_msg, decoded, mux_value=mux_value_int)
            self._emit_signals(event, signals, timestamp_ns, f"mux:{dbc_msg.name}/{mux_value_str}")
        # Note: Silently skip undefined mux values - this is valid during testing/development

    def _convert_signals(
        self,
        dbc_msg: cantools.database.can.Message,
        decoded: dict,
        base_only: bool = False,
        mux_value: int | None = None,
    ) -> dict:
        """Convert decoded signals to native Python types, filtered by category.

        :param dbc_msg: DBC message definition
        :param decoded: Decoded signal values from cantools
        :param base_only: If True, only include base (non-multiplexed) signals
        :param mux_value: If set, only include signals for this mux value
        :return: Dictionary of signal_name -> value
        """
        signals = {}
        for signal_name, value in decoded.items():
            signal_def = dbc_msg.get_signal_by_name(signal_name)

            if base_only:
                if signal_def.multiplexer_ids:
                    continue
            elif mux_value is not None and (
                not signal_def.multiplexer_ids or mux_value not in signal_def.multiplexer_ids
            ):
                continue

            if isinstance(value, int | float):
                # Trim fp64 noise to scale precision so 1234*0.001 ==
                # 1.2340000000000002 rounds to 1.234. Without this, the
                # webapp's string-based value-table lookup misses entries
                # like "1.234": "SNA", and the trace shows misleading
                # sub-scale noise.
                scale = float(signal_def.scale) if signal_def.scale is not None else 1.0
                precision = _scale_precision(scale)
                signals[signal_name] = round(value, precision) if precision > 0 else value
            else:
                # Defensive fallback. With decode_choices=False set on the
                # decode() call, cantools should never hand us a
                # NamedSignalValue here — but if it does (cantools internals
                # change), fall back to the raw int so we still emit
                # *something* numeric to the trace.
                signals[signal_name] = int(signal_def.conversion.choice_to_number(value))

        return signals

    # ─── Operations exposed by the free-floating actions module ────────────
    #
    # These methods are the implementation backing the global `can/<name>`
    # action surface defined in `zelos_extension_can.actions`. They're kept as
    # plain methods (no @action decorators) so `actions.py` can hold the
    # decorator stack and `choices=_available_codecs` lives at module scope.

    def _native_rx_counts(self) -> dict[str, int]:
        """RX counters for the native path, from the live Rust codec or the
        snapshot taken at stop. Counters are 0 before start."""
        if self._native is not None:
            m = self._native.metrics()
            return {
                "messages_received": m.messages_received,
                "messages_decoded": m.messages_decoded,
                "unknown_messages": m.unknown_messages,
            }
        if self._native_metrics is not None:
            return self._native_metrics
        return {"messages_received": 0, "messages_decoded": 0, "unknown_messages": 0}

    def _native_tx_counts(self) -> dict[str, int]:
        """TX counters for the Rust path, from the live codec or the stop-time
        snapshot. A stalled ssh transport surfaces here (the Rust codec's TX
        channel/outlet), not in the Python-side self.metrics. Counters are 0
        before start."""
        if self._native is not None:
            m = self._native.metrics()
            return {"tx_errors": m.tx_errors, "tx_overflows": m.tx_overflows}
        if self._native_tx_metrics is not None:
            return self._native_tx_metrics
        return {"tx_errors": 0, "tx_overflows": 0}

    def get_tx_state(self) -> dict[str, Any]:
        # Extension id/version/state intentionally NOT included — that info
        # is canonical at the `extensions.list` bridge surface and the webapp
        # consumes it from there, not from this 1 Hz polled action.
        db_path = Path(self.database_file_path)
        # On the Rust paths (zelos-socketcan / ssh-socketcan) RX counters live in
        # the Rust codec. TX counters merge the Python-side self.metrics (one-shot
        # send failures via the bus/adapter) with the Rust codec's own tx counters
        # (a stalled ssh transport surfaces there, not in self.metrics). For the
        # native zelos-socketcan path TX goes through a separate python-can compat
        # bus so the Rust tx counters stay 0 — the reported values are unchanged.
        tx_errors = self.metrics.tx_errors
        tx_overflows = self.metrics.tx_overflows
        if self._use_rust:
            rx = self._native_rx_counts()
            native_tx = self._native_tx_counts()
            tx_errors += native_tx["tx_errors"]
            tx_overflows += native_tx["tx_overflows"]
        else:
            rx = {
                "messages_received": self.metrics.messages_received,
                "messages_decoded": self.metrics.messages_decoded,
                "unknown_messages": self.metrics.unknown_messages,
            }
        return {
            "captured_at_unix_ms": int(time.time() * 1000),
            "bus": {
                "name": self.bus_name or "can_codec",
                "interface": self.config.get("interface", "unknown"),
                "channel": self.config.get("channel"),
                "status": _derive_bus_status(self.running, self.bus),
                "dbc": {
                    "path": str(db_path),
                    "name": db_path.name,
                    "hash": self.dbc_hash,
                    "message_count": len(self.db.messages),
                },
                "metrics": {
                    "tx_errors": tx_errors,
                    "tx_overflows": tx_overflows,
                    **rx,
                },
                "periodics": [self._periodic_slots[tid] for tid in sorted(self._periodic_slots)],
            },
        }

    def list_messages(self) -> dict[str, Any]:
        db_path = Path(self.database_file_path)
        return {
            "bus": self.bus_name or "can_codec",
            "dbc_name": db_path.name,
            "messages": [_describe_dbc_message_summary(msg) for msg in self.db.messages],
        }

    def describe_message(self, message: str) -> dict[str, Any]:
        dbc_msg = self._resolve_dbc_message(message)
        db_path = Path(self.database_file_path)
        return {
            "bus": self.bus_name or "can_codec",
            "dbc_name": db_path.name,
            "message": _describe_dbc_message(dbc_msg),
        }

    def send_raw(
        self,
        can_id: str,
        data: str,
        is_extended: bool = False,
        is_fd: bool = False,
    ) -> dict[str, Any]:
        self._require_running()
        can_id_int = _parse_can_id(can_id)
        _validate_id_range(can_id_int, is_extended)
        data_bytes = _parse_data_hex(data)
        msg = can.Message(
            arbitration_id=can_id_int,
            data=data_bytes,
            is_extended_id=is_extended,
            is_fd=is_fd,
        )
        self._send_or_count(msg)
        return {
            "can_id": can_id_int,
            "can_id_hex": f"0x{can_id_int:x}",
            "dlc": len(data_bytes),
            "data_hex": data_bytes.hex(),
            "is_extended": is_extended,
            "is_fd": is_fd,
        }

    def start_periodic_raw(
        self,
        can_id: str,
        data: str,
        period_ms: int = 100,
        is_extended: bool = False,
        is_fd: bool = False,
    ) -> dict[str, Any]:
        self._require_running()
        can_id_int = _parse_can_id(can_id)
        _validate_id_range(can_id_int, is_extended)
        data_bytes = _parse_data_hex(data)
        tid = _task_id(can_id_int, is_extended, "raw")
        replaced = self._stop_slot_if_present(tid)
        msg = can.Message(
            arbitration_id=can_id_int,
            data=data_bytes,
            is_extended_id=is_extended,
            is_fd=is_fd,
        )
        self._spawn_periodic(tid, msg, period_ms / 1000.0, mode="raw")
        self._periodic_slots[tid] = {
            "task_id": tid,
            "can_id": can_id_int,
            "is_extended": is_extended,
            "is_fd": is_fd,
            "dlc": len(data_bytes),
            "data_hex": data_bytes.hex(),
            "period_ms": period_ms,
            "mode": "raw",
            "is_active": True,
        }
        return {"task_id": tid, "replaced": replaced}

    def send_message(self, message: str, signals_json: str, mux: str = "") -> dict[str, Any]:
        self._require_running()
        signals = _parse_signals_json(signals_json)
        dbc_msg = self._resolve_dbc_message(message)
        mux_value = _parse_mux(mux)
        data_bytes = _encode_dbc(dbc_msg, signals, mux_value)
        msg = can.Message(
            arbitration_id=dbc_msg.frame_id,
            data=data_bytes,
            is_extended_id=dbc_msg.is_extended_frame,
        )
        self._send_or_count(msg)
        return {
            "message": message,
            "can_id": dbc_msg.frame_id,
            "can_id_hex": f"0x{dbc_msg.frame_id:x}",
            "dlc": len(data_bytes),
            "data_hex": data_bytes.hex(),
            "mux": mux_value,
        }

    def encode_preview(self, message: str, signals_json: str, mux: str = "") -> dict[str, Any]:
        signals = _parse_signals_json(signals_json)
        dbc_msg = self._resolve_dbc_message(message)
        mux_value = _parse_mux(mux)
        data_bytes = _encode_dbc(dbc_msg, signals, mux_value)
        return {
            "message": message,
            "can_id": dbc_msg.frame_id,
            "can_id_hex": f"0x{dbc_msg.frame_id:x}",
            "dlc": len(data_bytes),
            "data_hex": data_bytes.hex(),
            "mux": mux_value,
        }

    def start_periodic_message(
        self,
        message: str,
        signals_json: str,
        period_ms: int = 100,
        mux: str = "",
    ) -> dict[str, Any]:
        self._require_running()
        signals = _parse_signals_json(signals_json)
        dbc_msg = self._resolve_dbc_message(message)
        mux_value = _parse_mux(mux)
        data_bytes = _encode_dbc(dbc_msg, signals, mux_value)
        mux_key = "dbc" if mux_value is None else f"mux={mux_value}"
        tid = _task_id(dbc_msg.frame_id, dbc_msg.is_extended_frame, mux_key)
        replaced = self._stop_slot_if_present(tid)
        msg = can.Message(
            arbitration_id=dbc_msg.frame_id,
            data=data_bytes,
            is_extended_id=dbc_msg.is_extended_frame,
        )
        self._spawn_periodic(tid, msg, period_ms / 1000.0, mode="dbc")
        self._periodic_slots[tid] = {
            "task_id": tid,
            "can_id": dbc_msg.frame_id,
            "is_extended": dbc_msg.is_extended_frame,
            "is_fd": False,
            "dlc": len(data_bytes),
            "data_hex": data_bytes.hex(),
            "period_ms": period_ms,
            "mode": "dbc",
            "is_active": True,
            "message": {"name": message, "mux": mux_value, "signals": signals},
        }
        return {"task_id": tid, "replaced": replaced}

    def stop_periodic(self, task_id: str) -> dict[str, Any]:
        stopped = self._stop_slot_if_present(task_id)
        return {"task_id": task_id, "stopped": stopped}

    # ─── Internals shared by the action methods above ────────────────────

    def _require_running(self) -> None:
        if not self.running or not self.bus:
            raise RuntimeError(f"bus '{self.bus_name or 'can_codec'}' is not running")

    def _resolve_dbc_message(self, message: str) -> cantools.database.can.Message:
        dbc_msg = self.messages_by_name.get(message)
        if dbc_msg is None:
            available = sorted(self.messages_by_name.keys())
            preview = available[:20]
            raise ValueError(f"unknown DBC message '{message}'. First 20 available: {preview}")
        return dbc_msg

    def _spawn_periodic(self, tid: str, msg: can.Message, period_s: float, mode: str) -> None:
        task = self.bus.send_periodic(msg, period_s, autostart=True)
        self._periodic_tasks[tid] = task
        logger.info("started periodic %s mode=%s period=%.3fs", tid, mode, period_s)

    def _stop_slot_if_present(self, tid: str) -> bool:
        task = self._periodic_tasks.pop(tid, None)
        self._periodic_slots.pop(tid, None)
        if task is None:
            return False
        task.stop()
        logger.info("stopped periodic %s", tid)
        return True

    def _send_or_count(self, msg: can.Message) -> None:
        """Wrapper around bus.send() that counts CanError as tx_errors and
        re-raises with a friendlier message. Used by the one-shot send_raw /
        send_message paths (periodics go through python-can's CyclicSendTask
        which runs its own thread and doesn't surface errors back to us)."""
        try:
            self.bus.send(msg)
        except can.CanError as e:
            self.metrics.tx_errors += 1
            bus_name = self.bus_name or "can_codec"
            raise RuntimeError(f"send failed on bus '{bus_name}': {e}") from e
