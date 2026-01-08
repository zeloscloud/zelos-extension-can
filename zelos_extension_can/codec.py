"""CAN bus codec with database decoding and transmission."""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

import can
import cantools
import zelos_sdk
from zelos_sdk.actions import action

from .demo.demo import run_demo_ev_simulation
from .utils.schema_utils import cantools_signal_to_trace_metadata

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Metrics:
    """Performance metrics for CAN codec operations."""

    messages_received: int = 0
    messages_decoded: int = 0
    decode_errors: int = 0
    unknown_messages: int = 0


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

            self.raw_event = self.raw_source.add_event(
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
        else:
            self.raw_source = None
            self.raw_event = None

        # Build message lookup tables (handle duplicates permissively)
        self.messages_by_id: dict[int, cantools.db.can.Message] = {}
        self.messages_by_name: dict[str, cantools.db.can.Message] = {}

        self._events: dict[int, Any] = {}

        for msg in self.db.messages:
            self.messages_by_id[msg.frame_id] = msg
            # Only store first occurrence of duplicate names
            if msg.name not in self.messages_by_name:
                self.messages_by_name[msg.name] = msg
            else:
                logger.warning(
                    f"Duplicate message name '{msg.name}' (ID {msg.frame_id}), "
                    "access via message ID instead"
                )

        if self.emit_schemas_on_init:
            self._generate_all_schemas()
            logger.info("Generated %d event schemas from database", len(self._events))
        else:
            logger.info(
                "Schema generation deferred - will emit schemas as messages are encountered"
            )

        # Log raw frame configuration
        if self.log_raw_frames:
            logger.info(
                "Raw CAN frame logging is ENABLED - frames will be logged to 'can_raw' trace source"
            )
        else:
            logger.info("Raw CAN frame logging is DISABLED")

        self.bus: Any = None
        self.periodic_tasks: dict[str, asyncio.Task] = {}

    def _get_event_name(self, msg: cantools.database.can.Message) -> str:
        """Get event name for message (format: {frame_id:04x}_{name}).

        :param msg: cantools message
        :return: Event name string
        """
        return f"{msg.frame_id:04x}_{msg.name}"

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

    def start(self) -> None:
        """Initialize CAN bus connection with retry logic."""
        bus_id = f"[{self.bus_name}] " if self.bus_name else ""
        logger.info(
            f"{bus_id}Starting CAN bus: interface={self.config['interface']}, "
            f"channel={self.config['channel']}"
        )

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
                import json

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

    def stop(self) -> None:
        """Stop CAN bus and periodic tasks."""
        bus_id = f"[{self.bus_name}] " if self.bus_name else ""
        logger.info(f"{bus_id}Stopping CAN codec")
        self.running = False

        if self.demo_task:
            self.demo_task.cancel()
            self.demo_task = None

        for task_name, task in self.periodic_tasks.items():
            logger.info("Cancelling periodic task: %s", task_name)
            task.cancel()

        self.periodic_tasks.clear()

        if self.bus:
            self.bus.shutdown()
            self.bus = None

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
            decoded = self.db.decode_message(msg.arbitration_id, msg.data)
            self.metrics.messages_decoded += 1

            dbc_msg = self.messages_by_id.get(msg.arbitration_id)
            if not dbc_msg:
                logger.debug("Unknown message ID: %04x", msg.arbitration_id)
                self.metrics.unknown_messages += 1
                return

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
        cache_key = dbc_msg.frame_id
        event_name = self._get_event_name(dbc_msg)
        base_signals = [sig for sig in dbc_msg.signals if not sig.multiplexer_ids]

        if base_signals:
            fields = [cantools_signal_to_trace_metadata(sig) for sig in base_signals]
            event = self.source.add_event(event_name, fields)

            for sig in base_signals:
                if sig.choices:
                    value_table = {int(k): str(v) for k, v in sig.choices.items()}
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

        cache_key = (dbc_msg.frame_id, mux_value_int)

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
                if sig.choices:
                    value_table = {int(k): str(v) for k, v in sig.choices.items()}
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
        cache_key = dbc_msg.frame_id
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

        cache_key = (dbc_msg.frame_id, mux_value_int)
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
                signals[signal_name] = value
            else:
                # NamedSignalValue - convert to integer
                signals[signal_name] = int(signal_def.conversion.choice_to_number(value))

        return signals

    async def _periodic_send_task(
        self, msg_id: int, data: bytes, period: float, task_name: str, extended_id: bool = False
    ) -> None:
        """Periodic message transmission task.

        :param msg_id: CAN message ID
        :param data: Message data bytes
        :param period: Period in seconds
        :param task_name: Task identifier
        :param extended_id: Use 29-bit extended ID
        """
        try:
            logger.info(
                "Starting periodic transmission: %s (ID: %04x, period: %ss, extended: %s)",
                task_name,
                msg_id,
                period,
                extended_id,
            )

            while self.running:
                if not self.bus:
                    logger.warning("Bus not available for periodic task %s", task_name)
                    await asyncio.sleep(period)
                    continue

                try:
                    msg = can.Message(
                        arbitration_id=msg_id,
                        data=data,
                        is_extended_id=extended_id,
                        is_fd=self.fd_mode,
                    )
                    self.bus.send(msg)
                except can.CanError as e:
                    logger.error("Failed to send periodic message %s: %s", task_name, e)
                except Exception as e:
                    logger.error("Unexpected error in periodic send %s: %s", task_name, e)

                await asyncio.sleep(period)
        except asyncio.CancelledError:
            logger.info("Periodic task cancelled: %s", task_name)
        except Exception as e:
            logger.exception("Error in periodic task %s: %s", task_name, e)

    @action("Get Status", "View CAN bus status")
    def get_status(self) -> dict[str, Any]:
        """Get current CAN bus status.

        :return: Status information
        """
        bus_state = "not_initialized" if not self.bus else str(self.bus.state.name)

        return {
            "bus_state": bus_state,
            "running": self.running,
            "interface": self.config["interface"],
            "channel": self.config["channel"],
            "fd_mode": self.fd_mode,
        }

    @action("Send Message", "Send a single CAN message")
    @action.number("msg_id", minimum=0, maximum=0x1FFFFFFF, title="Message ID", default=0x100)
    @action.text("data", title="Data (hex bytes)", placeholder="01 02 03 04", default="00")
    @action.boolean("extended_id", title="Extended ID (29-bit)", default=False, widget="toggle")
    def send_message(self, msg_id: int, data: str, extended_id: bool = False) -> dict[str, Any]:
        """Send a CAN message.

        :param msg_id: CAN message ID (11-bit standard or 29-bit extended)
        :param data: Hex data string (e.g., "01 02 03")
        :param extended_id: Use 29-bit extended ID
        :return: Confirmation message
        """
        if not self.bus:
            return {"error": "CAN bus not started"}

        # Validate ID range
        max_id = 0x1FFFFFFF if extended_id else 0x7FF
        if msg_id > max_id:
            id_type = "extended" if extended_id else "standard"
            return {"error": f"Message ID {msg_id:x} exceeds max for {id_type} ID ({max_id:x})"}

        try:
            data_bytes = bytes.fromhex(data.replace(" ", ""))
            is_fd = self.fd_mode

            msg = can.Message(
                arbitration_id=msg_id, data=data_bytes, is_extended_id=extended_id, is_fd=is_fd
            )
            self.bus.send(msg)
            logger.info(
                f"Sent message: ID={msg_id:04x}, data={data_bytes.hex()}, extended={extended_id}"
            )
            return {
                "status": "sent",
                "id": f"0x{msg_id:04x}" if not extended_id else f"0x{msg_id:08x}",
                "data": data_bytes.hex(),
                "extended_id": extended_id,
            }
        except can.CanError as e:
            logger.error("CAN error sending message: %s", e)
            return {"error": f"CAN error: {e}"}
        except Exception as e:
            logger.error("Error sending message: %s", e)
            return {"error": str(e)}

    @action("Start Periodic Message", "Start periodic transmission of a CAN message")
    @action.number("msg_id", minimum=0, maximum=0x1FFFFFFF, title="Message ID", default=0x100)
    @action.text("data", title="Data (hex)", placeholder="01 02 03 04", default="00")
    @action.number("period", minimum=0.001, maximum=10.0, title="Period (seconds)", default=0.1)
    @action.boolean("extended_id", title="Extended ID (29-bit)", default=False, widget="toggle")
    def start_periodic(
        self, msg_id: int, data: str, period: float, extended_id: bool = False
    ) -> dict[str, Any]:
        """Start periodic transmission of a message.

        :param msg_id: CAN message ID
        :param data: Hex data string
        :param period: Transmission period in seconds
        :param extended_id: Use 29-bit extended ID
        :return: Confirmation message
        """
        if not self.bus or not self.running:
            return {"error": "CAN bus not running"}

        # Validate ID range
        max_id = 0x1FFFFFFF if extended_id else 0x7FF
        if msg_id > max_id:
            id_type = "extended" if extended_id else "standard"
            return {"error": f"Message ID {msg_id:x} exceeds max for {id_type} ID ({max_id:x})"}

        try:
            data_bytes = bytes.fromhex(data.replace(" ", ""))
            task_name = f"periodic_{msg_id:08x}" if extended_id else f"periodic_{msg_id:04x}"

            # Cancel existing task if present
            if task_name in self.periodic_tasks:
                self.periodic_tasks[task_name].cancel()
                logger.info("Cancelled existing periodic task: %s", task_name)

            # Create new periodic task
            task = asyncio.create_task(
                self._periodic_send_task(msg_id, data_bytes, period, task_name, extended_id)
            )
            self.periodic_tasks[task_name] = task

            return {
                "status": "started",
                "task_name": task_name,
                "id": f"0x{msg_id:04x}" if not extended_id else f"0x{msg_id:08x}",
                "period": period,
                "extended_id": extended_id,
            }
        except Exception as e:
            logger.error("Error starting periodic transmission: %s", e)
            return {"error": str(e)}

    @action("Stop Periodic Message", "Stop periodic transmission")
    @action.text("task_name", title="Task Name", placeholder="periodic_0100")
    def stop_periodic(self, task_name: str) -> dict[str, Any]:
        """Stop periodic transmission of a message.

        :param task_name: Task name (from list_periodic_tasks)
        :return: Confirmation message
        """
        if task_name in self.periodic_tasks:
            self.periodic_tasks[task_name].cancel()
            del self.periodic_tasks[task_name]
            logger.info("Stopped periodic task: %s", task_name)
            return {"status": "stopped", "task_name": task_name}
        else:
            return {"error": f"No periodic task found: {task_name}"}

    @action("List Periodic Tasks", "Show all active periodic transmissions")
    def list_periodic_tasks(self) -> dict[str, Any]:
        """List all active periodic transmission tasks.

        :return: Dictionary of active tasks
        """
        tasks = []
        for name, task in self.periodic_tasks.items():
            tasks.append(
                {
                    "name": name,
                    "running": not task.done(),
                    "cancelled": task.cancelled(),
                }
            )

        return {"count": len(tasks), "tasks": tasks}

    @action("Get Metrics", "View performance metrics and statistics")
    def get_metrics(self) -> dict[str, Any]:
        """Get codec performance metrics.

        :return: Performance metrics
        """
        uptime = time.time() - self.start_time
        messages_received = self.metrics.messages_received

        return {
            "messages_received": self.metrics.messages_received,
            "messages_decoded": self.metrics.messages_decoded,
            "decode_errors": self.metrics.decode_errors,
            "unknown_messages": self.metrics.unknown_messages,
            "uptime_seconds": round(uptime, 2),
            "messages_per_second": round(messages_received / max(uptime, 1), 2),
            "decode_success_rate": round(
                self.metrics.messages_decoded / max(messages_received, 1), 4
            ),
        }

    @action("List Messages", "List all messages in database")
    def list_messages(self) -> dict[str, Any]:
        """List all CAN messages in loaded database file.

        :return: Dictionary of message information
        """
        messages = []
        for msg in self.db.messages:
            messages.append(
                {
                    "id": f"0x{msg.frame_id:04x}",
                    "name": msg.name,
                    "length": msg.length,
                    "signals": len(msg.signals),
                }
            )

        return {"count": len(messages), "messages": messages}

    @action("Convert Trace File", "Convert CAN log to Zelos trace format")
    @action.text(
        "input_path",
        title="Input File Path",
        description="Path to CAN log file (.asc, .blf, .trc, etc.)",
        widget="file-picker",
    )
    @action.text(
        "output_path",
        required=False,
        default="",
        title="Output File Path",
        description="Output .trz file path (optional, defaults to input name with .trz)",
        placeholder="e.g., /path/to/output.trz",
    )
    @action.text(
        "database_path",
        required=False,
        default="",
        title="CAN Database File (.dbc)",
        description="Override database file (optional, defaults to extension's configured file)",
        placeholder="Leave empty to use extension's database",
        widget="file-picker",
    )
    @action.boolean(
        "overwrite", required=False, default=False, title="Overwrite if exists", widget="toggle"
    )
    def convert_trace_file(
        self,
        input_path: str,
        output_path: str = "",
        database_path: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Convert CAN trace file to Zelos format using CAN database file.

        :param input_path: Path to CAN log file
        :param output_path: Output .trz file path (optional)
        :param database_path: Database file path (.dbc, .arxml, etc.) - defaults to extension's file
        :param overwrite: Overwrite existing output file
        :return: Conversion result with statistics
        """
        from pathlib import Path

        from .converter import convert_can_trace

        try:
            # Validate input path
            input_file = Path(input_path).expanduser().resolve()
            if not input_file.exists():
                return {
                    "status": "error",
                    "message": f"Input file not found: {input_file}",
                }

            # Determine database file to use (parameter override or extension's configured file)
            if database_path:
                # User provided a database file path - handle it
                database_file = Path(database_path).expanduser().resolve()
                if not database_file.exists():
                    return {
                        "status": "error",
                        "message": f"CAN database file not found: {database_file}",
                    }
            else:
                # Use extension's already-loaded database file path (already resolved)
                if not hasattr(self, "database_file_path") or not self.database_file_path:
                    return {
                        "status": "error",
                        "message": "No database file specified and extension has none configured",
                    }
                database_file = Path(self.database_file_path)

            # Determine output path
            if not output_path:
                output_path = str(input_file.with_suffix(".trz"))

            output_file = Path(output_path).expanduser().resolve()

            # Ensure output always has .trz extension
            if output_file.suffix.lower() != ".trz":
                output_file = output_file.with_suffix(".trz")

            # Safety check: prevent overwriting input file
            if output_file == input_file:
                return {
                    "status": "error",
                    "message": f"Output file cannot be the same as input file: {input_file}",
                }

            # Check if output exists
            if output_file.exists():
                if overwrite:
                    logger.info("Removing existing file: %s", output_file)
                    output_file.unlink()
                else:
                    return {
                        "status": "error",
                        "message": f"Output file '{output_file}' already exists. "
                        "Enable 'Overwrite if exists' to replace it.",
                    }

            # Perform conversion
            logger.info(
                "Converting %s -> %s using database: %s", input_file, output_file, database_file
            )
            stats = convert_can_trace(input_file, database_file, output_file)

            return {
                "status": "success",
                "input_file": str(input_file),
                "database_file": str(database_file),
                "output_file": str(output_file),
                **stats.to_dict(),
            }

        except FileNotFoundError as e:
            return {"status": "error", "message": f"File not found: {e}"}
        except ValueError as e:
            return {"status": "error", "message": f"Invalid input: {e}"}
        except ImportError as e:
            return {"status": "error", "message": f"Missing dependency: {e}"}
        except Exception as e:
            logger.exception("Conversion failed")
            return {"status": "error", "message": f"Conversion failed: {e}"}
