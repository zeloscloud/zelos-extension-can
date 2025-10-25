"""CAN bus codec with DBC decoding and transmission."""

import asyncio
import logging
import time
from enum import IntEnum
from pathlib import Path
from typing import Any

import can
import cantools
import zelos_sdk
from zelos_sdk.actions import action

from .demo.demo import run_demo_ev_simulation
from .schema_utils import cantools_signal_to_trace_metadata
from .utils.config import data_url_to_file

logger = logging.getLogger(__name__)


class TimestampMode(IntEnum):
    """Timestamp handling modes for efficient comparison."""

    IGNORE = 0
    ABSOLUTE = 1
    AUTO = 2


class CanCodec(can.Listener):
    """CAN bus monitor with DBC decoding and periodic transmission support."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize CAN codec.

        :param config: Configuration dictionary with interface, channel, dbc_file
        """
        self.config = config
        self.running = False
        self.last_message_time = time.time()
        self.start_time = time.time()

        # Timestamp handling - use enum for fast comparison
        timestamp_mode_str = config.get("timestamp_mode", "auto").upper()
        self.timestamp_mode = TimestampMode[timestamp_mode_str]
        self.hw_timestamp_offset: float | None = None  # Offset to convert HW time to wall-clock
        self.first_hw_timestamp: float | None = None  # First HW timestamp seen

        # Metrics tracking
        self.metrics = {
            "messages_received": 0,
            "messages_decoded": 0,
            "decode_errors": 0,
            "unknown_messages": 0,
            "bytes_received": 0,
        }

        # Demo mode simulation
        self.demo_mode = config.get("demo_mode", False)
        self.demo_task: asyncio.Task | None = None

        # Load and validate DBC file (handle data-url or plain file path)
        dbc_value = config["dbc_file"]

        if dbc_value.startswith("data:"):
            logger.info("Extracting uploaded DBC file from data-url")
            dbc_path = data_url_to_file(dbc_value, ".uploaded.dbc")
        else:
            dbc_path = dbc_value

        if not Path(dbc_path).exists():
            raise FileNotFoundError(f"DBC file not found: {dbc_path}")

        logger.info(f"Loading DBC file: {dbc_path}")
        try:
            self.db = cantools.database.load_file(dbc_path)
            logger.info(f"Loaded {len(self.db.messages)} messages from DBC")
        except Exception as e:
            raise ValueError(f"Failed to load DBC file: {e}") from e

        # Create trace source
        self.source = zelos_sdk.TraceSource("can")

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

        self._generate_all_schemas()
        logger.info(f"Generated {len(self._events)} event schemas from DBC")

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

        # Auto mode: detect boot-relative timestamps and calculate offset
        if self.hw_timestamp_offset is None:
            self.first_hw_timestamp = hw_timestamp

            # Timestamps < 1 hour are assumed to be boot-relative and need conversion
            ONE_HOUR = 3600.0
            if hw_timestamp < ONE_HOUR:
                wall_clock_time = time.time()
                self.hw_timestamp_offset = wall_clock_time - hw_timestamp
                logger.info(
                    f"Detected boot-relative timestamps (first={hw_timestamp:.3f}s). "
                    f"Using offset={self.hw_timestamp_offset:.3f}s to convert to wall-clock time."
                )
            else:
                self.hw_timestamp_offset = 0.0
                logger.info(
                    f"Detected absolute timestamps (first={hw_timestamp:.3f}s). "
                    "Using hardware timestamps as-is."
                )

        # Apply offset to convert boot-relative time to wall-clock time
        wall_clock_timestamp = hw_timestamp + self.hw_timestamp_offset
        return int(wall_clock_timestamp * 1e9)

    def start(self) -> None:
        """Initialize CAN bus connection with retry logic."""
        logger.info(
            f"Starting CAN bus: interface={self.config['interface']}, "
            f"channel={self.config['channel']}"
        )

        bus_config = {
            "interface": self.config["interface"],
            "channel": self.config["channel"],
            "receive_own_messages": True,  # Allow receiving own transmitted messages
        }

        if self.config["interface"] != "virtual":
            bus_config["bitrate"] = self.config.get("bitrate", 500000)

        if self.config.get("fd_mode", False):
            bus_config["fd"] = True
            bus_config["data_bitrate"] = self.config.get("data_bitrate", 2000000)

        # Merge additional config_json (advanced interface-specific options)
        if "config_json" in self.config and self.config["config_json"]:
            try:
                import json

                additional_config = json.loads(self.config["config_json"])
                logger.info(f"Merging additional config: {list(additional_config.keys())}")
                bus_config.update(additional_config)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse config_json: {e}")
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
                    logger.error(f"Failed to initialize CAN bus after {max_retries} attempts")
                    raise
                logger.warning(f"Bus init failed (attempt {attempt + 1}/{max_retries}): {e}")
                time.sleep(1)

    def stop(self) -> None:
        """Stop CAN bus and periodic tasks."""
        logger.info("Stopping CAN codec")
        self.running = False

        if self.demo_task:
            logger.info("Cancelling demo simulation task")
            self.demo_task.cancel()
            self.demo_task = None

        for task_name, task in self.periodic_tasks.items():
            logger.info(f"Cancelling periodic task: {task_name}")
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
            return False

        # Virtual/demo interfaces don't support state checks - assume healthy
        if self.config.get("interface") == "virtual" or self.demo_mode:
            return True

        try:
            state = self.bus.state
            return state in (can.BusState.ACTIVE, can.BusState.PASSIVE)
        except Exception as e:
            logger.debug(f"Bus health check failed: {e}")
            # If state check not supported, assume healthy
            return True

    async def _reconnect_bus(self) -> bool:
        """Attempt to reconnect to CAN bus.

        :return: True if reconnection successful
        """
        logger.warning("Attempting bus reconnection...")
        try:
            if self.bus:
                self.bus.shutdown()
                self.bus = None

            await asyncio.sleep(1)
            self.start()
            logger.info("Bus reconnected successfully")
            return True
        except Exception as e:
            logger.error(f"Bus reconnection failed: {e}")
            return False

    def on_message_received(self, message: can.Message) -> None:
        """Handle CAN message directly from notifier (can.Listener interface).

        This direct callback approach is more efficient than AsyncBufferedReader
        as it eliminates buffering overhead and async context switching.

        :param message: Received CAN message
        """
        self._handle_message(message)
        self.last_message_time = time.time()

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
            logger.info("Starting CAN message reception with direct listener pattern")
            while self.running:
                await asyncio.sleep(5.0)

                if not self._check_bus_health():
                    logger.error("Bus health check failed")
                    notifier.stop()
                    if await self._reconnect_bus():
                        notifier = can.Notifier(self.bus, [self])
                    else:
                        logger.error("Failed to reconnect, stopping")
                        break
        except asyncio.CancelledError:
            logger.info("CAN reader cancelled")
        except Exception as e:
            logger.exception(f"Error in CAN reception loop: {e}")
        finally:
            notifier.stop()
            logger.info("CAN reception stopped")

    def _handle_message(self, msg: can.Message) -> None:
        """Decode and emit CAN message to trace.

        For multiplexed messages, emits TWO separate events to minimize memory footprint:
        1. Base signals (including multiplexer): {id:04x}_{name}
        2. Multiplexed signals: {id:04x}_{name}/{mux_value}

        :param msg: Received CAN message
        """
        self.metrics["messages_received"] += 1
        self.metrics["bytes_received"] += len(msg.data)

        max_dlc = 64 if self.config.get("fd_mode", False) else 8
        if msg.dlc > max_dlc:
            logger.warning(
                f"Invalid DLC {msg.dlc} for message {msg.arbitration_id:04x} (max: {max_dlc})"
            )
            return

        try:
            decoded = self.db.decode_message(msg.arbitration_id, msg.data)

            dbc_msg = self.messages_by_id.get(msg.arbitration_id)
            if not dbc_msg:
                logger.debug(f"Unknown message ID: {msg.arbitration_id:04x}")
                self.metrics["unknown_messages"] += 1
                return

            # Get timestamp once for all emissions
            timestamp_ns = self.get_timestamp(msg.timestamp)

            # Emit base signals (non-multiplexed signals + multiplexer signal if present)
            self._emit_base_signals(dbc_msg, decoded, timestamp_ns)

            if dbc_msg.is_multiplexed():
                self._emit_multiplexed_signals(dbc_msg, decoded, timestamp_ns)

            self.metrics["messages_decoded"] += 1

        except KeyError:
            logger.debug(f"Message ID {msg.arbitration_id:04x} not in DBC")
            self.metrics["unknown_messages"] += 1
        except cantools.database.DecodeError as e:
            logger.debug(f"Decode error for {msg.arbitration_id:04x}: {e}")
            self.metrics["decode_errors"] += 1
        except Exception as e:
            logger.debug(f"Error decoding message {msg.arbitration_id:04x}: {e}")
            self.metrics["decode_errors"] += 1

    def _generate_all_schemas(self) -> None:
        """Generate trace event schemas for all messages in DBC at init time.

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
            logger.debug(f"Generated base schema: '{event_name}' ({len(fields)} signals)")

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
            # Use enum name if available, otherwise stringified integer
            if mux_signal.choices and mux_value_int in mux_signal.choices:
                mux_value_str = mux_signal.choices[mux_value_int]
            else:
                mux_value_str = str(mux_value_int)

            cache_key = (dbc_msg.frame_id, mux_value_int)
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
                logger.debug(f"Generated mux schema: '{event_name}' ({len(fields)} signals)")

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
            logger.debug(f"Emitted {context}: {signals}")
        except (OverflowError, ValueError) as e:
            logger.debug(f"Skipping emission for {context}: {e}")
            self.metrics["decode_errors"] += 1

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

        if isinstance(mux_value, (int, float)):
            mux_value_int = int(mux_value)
        else:
            # NamedSignalValue - get integer representation
            mux_value_int = int(mux_signal.conversion.choice_to_number(mux_value))

        cache_key = (dbc_msg.frame_id, mux_value_int)
        event = self._events.get(cache_key)

        if event:
            # Get string representation for debug logging
            if isinstance(mux_value, (int, float)):
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

            if isinstance(value, (int, float)):
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
                f"Starting periodic transmission: {task_name} (ID: {msg_id:04x}, "
                f"period: {period}s, extended: {extended_id})"
            )
            is_fd = self.config.get("fd_mode", False)

            while self.running:
                if not self.bus:
                    logger.warning(f"Bus not available for periodic task {task_name}")
                    await asyncio.sleep(period)
                    continue

                try:
                    msg = can.Message(
                        arbitration_id=msg_id, data=data, is_extended_id=extended_id, is_fd=is_fd
                    )
                    self.bus.send(msg)
                except can.CanError as e:
                    logger.error(f"Failed to send periodic message {task_name}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error in periodic send {task_name}: {e}")

                await asyncio.sleep(period)
        except asyncio.CancelledError:
            logger.info(f"Periodic task cancelled: {task_name}")
        except Exception as e:
            logger.exception(f"Error in periodic task {task_name}: {e}")

    # --- Actions ---

    @action("Get Status", "View CAN bus status")
    def get_status(self) -> dict[str, Any]:
        """Get current CAN bus status.

        :return: Status information
        """
        return {
            "running": self.running,
            "interface": self.config["interface"],
            "channel": self.config["channel"],
            "messages_in_dbc": len(self.db.messages),
            "active_periodic_tasks": len(self.periodic_tasks),
            "fd_mode": self.config.get("fd_mode", False),
        }

    @action("Get Bus Health", "View detailed bus health metrics")
    def get_bus_health(self) -> dict[str, Any]:
        """Get detailed bus health information.

        :return: Health metrics
        """
        if not self.bus:
            return {"error": "Bus not initialized"}

        try:
            state = self.bus.state
            return {
                "state": str(state),
                "healthy": state in (can.BusState.ACTIVE, can.BusState.PASSIVE),
                "last_message_age_seconds": round(time.time() - self.last_message_time, 2),
            }
        except Exception as e:
            return {"error": f"Failed to get bus health: {e}"}

    @action("Send Message", "Send a single CAN message")
    @action.number("msg_id", minimum=0, maximum=0x1FFFFFFF, title="Message ID", default=0x100)
    @action.text("data", title="Data (hex bytes)", placeholder="01 02 03 04", default="00")
    @action.boolean("extended_id", title="Extended ID (29-bit)", default=False)
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
            is_fd = self.config.get("fd_mode", False)

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
            logger.error(f"CAN error sending message: {e}")
            return {"error": f"CAN error: {e}"}
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return {"error": str(e)}

    @action("Start Periodic Message", "Start periodic transmission of a CAN message")
    @action.number("msg_id", minimum=0, maximum=0x1FFFFFFF, title="Message ID", default=0x100)
    @action.text("data", title="Data (hex)", placeholder="01 02 03 04", default="00")
    @action.number("period", minimum=0.001, maximum=10.0, title="Period (seconds)", default=0.1)
    @action.boolean("extended_id", title="Extended ID (29-bit)", default=False)
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
                logger.info(f"Cancelled existing periodic task: {task_name}")

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
            logger.error(f"Error starting periodic transmission: {e}")
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
            logger.info(f"Stopped periodic task: {task_name}")
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
        messages_received = self.metrics["messages_received"]

        return {
            **self.metrics,
            "uptime_seconds": round(uptime, 2),
            "messages_per_second": round(messages_received / max(uptime, 1), 2),
            "decode_success_rate": round(
                self.metrics["messages_decoded"] / max(messages_received, 1), 4
            ),
            "bytes_per_second": round(self.metrics["bytes_received"] / max(uptime, 1), 2),
        }

    @action("List Messages", "List all messages in DBC")
    def list_messages(self) -> dict[str, Any]:
        """List all CAN messages in loaded DBC.

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
