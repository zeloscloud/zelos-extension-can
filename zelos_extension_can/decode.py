"""The python-can receive path: decode each frame under its DBC definitions
and emit it, and its raw form, into the trace."""

import logging
import time
from enum import IntEnum
from typing import TYPE_CHECKING, Any

import can
import cantools
import zelos_sdk

from .dbc import DbcCatalog, definition_key, scale_precision, value_table_for_trace
from .utils.schema_utils import cantools_signal_to_trace_metadata

if TYPE_CHECKING:
    from .codec import Metrics

logger = logging.getLogger(__name__)

#: Event type stamped on every decoded message table, base and mux, matching
#: what the Rust codec emits. A family type: the fields are the DBC message's
#: own, so it only marks the table as a CAN decode.
DECODED_EVENT_TYPE = "zelos.can.message.v1"


class TimestampMode(IntEnum):
    """Timestamp handling modes for efficient comparison."""

    IGNORE = 0
    ABSOLUTE = 1
    AUTO = 2


class FrameDecoder:
    """Decodes python-can frames under every definition of their id and emits
    them into the trace; the Rust codec does the same on the SocketCAN paths."""

    def __init__(
        self,
        catalog: DbcCatalog,
        source: Any,
        metrics: "Metrics",
        *,
        event_prefix: str | None,
        raw_event_name: str | None,
        timestamp_mode: TimestampMode,
        emit_schemas_on_init: bool,
    ) -> None:
        """
        :param event_prefix: Leading event-name segment under a shared source,
            or None when the bus owns its source
        :param raw_event_name: Event raw frames are logged to, or None to log none
        :param emit_schemas_on_init: Whether every schema was registered up
            front; otherwise each is registered on its first frame
        """
        self.catalog = catalog
        self.source = source
        self.metrics = metrics
        self._event_prefix = f"{event_prefix}/" if event_prefix else ""
        self.raw_event = (
            source.add_event(raw_event_name, zelos_sdk.schemas.CanFrame) if raw_event_name else None
        )
        self.timestamp_mode = timestamp_mode
        self.hw_timestamp_offset: float | None = None  # Offset to convert HW time to wall-clock
        self.first_hw_timestamp: float | None = None  # First HW timestamp seen
        self.emit_schemas_on_init = emit_schemas_on_init
        # Keyed per definition, plus the mux value for a subtable.
        self._events: dict[tuple[int, bool, str] | tuple[int, bool, str, int], Any] = {}
        # Definitions whose decode -> emit path raised an unexpected exception
        # (e.g. a schema that cannot be registered). Such faults are
        # deterministic: without this set a 100 Hz message would raise, and log,
        # 100 times a second forever.
        #
        # LOG-VOLUME INVARIANT (extension logs persist to disk, unwatched):
        # at most ONE log record per distinct failing definition per process
        # lifetime, and ZERO records on the short-circuit path once a key is in
        # this set. Worst case is therefore one ERROR per DBC message, ever.
        self._failed_messages: set[tuple[int, bool, str]] = set()

        if emit_schemas_on_init:
            self.generate_all_schemas()
            logger.info("Generated %d event schemas from database", len(self._events))
        else:
            logger.info(
                "Schema generation deferred - will emit schemas as messages are encountered"
            )

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

    def _get_event_name(self, msg: cantools.database.can.Message) -> str:
        """Trace event name for a message: its key under the bus prefix."""
        return f"{self._event_prefix}{self.catalog.key_of(msg)}"

    def handle(self, msg: can.Message) -> None:
        """Decode and emit CAN message to trace.

        One event per definition of the frame's id; for a multiplexed definition
        TWO, to minimize memory footprint:
        1. Base signals (including multiplexer): {id:04x}_{name}
        2. Multiplexed signals: {id:04x}_{name}/{mux_value}

        :param msg: Received CAN message
        """
        logger.debug("Received CAN message: %s", msg)
        self._update_receive_metrics(msg)
        timestamp_ns = self.get_timestamp(msg.timestamp)
        self._emit_raw_frame(msg, timestamp_ns)
        self._decode_and_emit_message(msg, timestamp_ns)

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
        if self.raw_event is None:
            return

        if timestamp_ns is None:
            self.raw_event.log(
                arbitration_id=msg.arbitration_id,
                is_extended=msg.is_extended_id,
                is_fd=msg.is_fd,
                is_rx=msg.is_rx,
                dlc=msg.dlc,
                data=msg.data,
            )
        else:
            self.raw_event.log_at(
                timestamp_ns,
                arbitration_id=msg.arbitration_id,
                is_extended=msg.is_extended_id,
                is_fd=msg.is_fd,
                is_rx=msg.is_rx,
                dlc=msg.dlc,
                data=msg.data,
            )

    def _decode_and_emit_message(self, msg: can.Message, timestamp_ns: int | None) -> None:
        """Decode a CAN frame under EVERY definition of its id and emit each
        into its own trace table.

        :param msg: CAN message
        :param timestamp_ns: Timestamp in nanoseconds
        """
        id_key = (msg.arbitration_id, msg.is_extended_id)
        dbc_msgs = self.catalog.messages_by_id.get(id_key)
        if not dbc_msgs:
            logger.debug(
                "Unknown message ID: %04x (extended=%s)", msg.arbitration_id, msg.is_extended_id
            )
            self.metrics.unknown_messages += 1
            return

        for dbc_msg in dbc_msgs:
            self._decode_one_definition(dbc_msg, msg, timestamp_ns)

    def _decode_one_definition(
        self,
        dbc_msg: cantools.database.can.Message,
        msg: can.Message,
        timestamp_ns: int | None,
    ) -> None:
        """Decode a frame under one definition. Counters and the failure
        blocklist are per definition, so a broken definition of an overlapping
        id never silences the others.

        :param dbc_msg: DBC message definition
        :param msg: CAN message
        :param timestamp_ns: Timestamp in nanoseconds
        """
        # Resolved before the try so the failure handler below always has a key
        # to blocklist — that is what bounds the log volume.
        key = definition_key(dbc_msg)

        # This definition already failed with an unexpected error, which is
        # deterministic, so skip the work. Silent by contract: the log-volume
        # invariant on self._failed_messages allows ZERO records here.
        if key in self._failed_messages:
            self.metrics.emit_errors += 1
            return

        try:
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
            # First unexpected failure for this message (schema registration,
            # signal conversion, ...). Loud once, then never again: the key is
            # blocklisted so every later frame takes the silent short-circuit
            # above, keeping the log-volume invariant.
            logger.error(
                "Failed to emit message %04x (%s): %s - suppressing further errors "
                "for this message",
                msg.arbitration_id,
                dbc_msg.name,
                e,
            )
            self._failed_messages.add(key)
            self.metrics.emit_errors += 1

    def generate_all_schemas(self) -> None:
        """Generate trace event schemas for all messages in database at init time.

        This provides visibility into what messages are defined, even before they're received.
        For multiplexed messages, generates schemas for all possible mux values.
        """
        for dbc_msg in self.catalog.messages:
            self._generate_base_schema(dbc_msg)

            if dbc_msg.is_multiplexed():
                self._generate_mux_schemas(dbc_msg)

    def _generate_base_schema(self, dbc_msg: cantools.database.can.Message) -> None:
        """Generate schema for base (non-multiplexed) signals.

        :param dbc_msg: DBC message definition
        """
        cache_key = definition_key(dbc_msg)
        event_name = self._get_event_name(dbc_msg)
        base_signals = [sig for sig in dbc_msg.signals if not sig.multiplexer_ids]

        if base_signals:
            fields = [cantools_signal_to_trace_metadata(sig) for sig in base_signals]
            event = self.source.add_event(event_name, fields, event_type=DECODED_EVENT_TYPE)

            for sig in base_signals:
                value_table = value_table_for_trace(sig)
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

        cache_key = (*definition_key(dbc_msg), mux_value_int)

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
            event = self.source.add_event(event_name, fields, event_type=DECODED_EVENT_TYPE)

            for sig in mux_signals:
                value_table = value_table_for_trace(sig)
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
        cache_key = definition_key(dbc_msg)
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

        cache_key = (*definition_key(dbc_msg), mux_value_int)
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
                precision = scale_precision(scale)
                signals[signal_name] = round(value, precision) if precision > 0 else value
            else:
                # Defensive fallback. With decode_choices=False set on the
                # decode() call, cantools should never hand us a
                # NamedSignalValue here — but if it does (cantools internals
                # change), fall back to the raw int so we still emit
                # *something* numeric to the trace.
                signals[signal_name] = int(signal_def.conversion.choice_to_number(value))

        return signals
