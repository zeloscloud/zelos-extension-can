"""CAN bus codec with DBC decoding and transmission."""

import asyncio
import logging
import os
import time
from typing import Any

import can
import cantools
import zelos_sdk
from zelos_sdk.actions import action

from .schema_utils import cantools_signal_to_trace_metadata

logger = logging.getLogger(__name__)


class CanCodec:
    """CAN bus monitor with DBC decoding and periodic transmission support."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize CAN codec.

        :param config: Configuration dictionary with interface, channel, dbc_file
        """
        self.config = config
        self.running = False
        self.last_message_time = time.time()
        self.start_time = time.time()

        # Metrics tracking
        self.metrics = {
            "messages_received": 0,
            "messages_decoded": 0,
            "decode_errors": 0,
            "unknown_messages": 0,
            "bytes_received": 0,
        }

        # Load and validate DBC file
        dbc_path = config["dbc_file"]
        if not os.path.exists(dbc_path):
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

        # Generate trace schema from DBC
        self._generate_schema()

        # CAN bus (initialized in start())
        self.bus: Any = None

        # Periodic message tasks
        self.periodic_tasks: dict[str, asyncio.Task] = {}

    def _generate_schema(self) -> None:
        """Generate trace event schemas from DBC messages."""
        for msg in self.db.messages:
            # Create event for each message
            # For multiplexed messages, we emit all signals in one event
            # The actual mux logic happens during decode
            event_name = self._get_event_name(msg)
            fields = [cantools_signal_to_trace_metadata(sig) for sig in msg.signals]

            if fields:
                self.source.add_event(event_name, fields)
                logger.debug(f"Added event '{event_name}' with {len(fields)} signals")

    def _get_event_name(self, msg: cantools.database.can.Message) -> str:
        """Get event name for message (format: {frame_id:04x}_{name}).

        :param msg: cantools message
        :return: Event name string
        """
        return f"{msg.frame_id:04x}_{msg.name}"

    def start(self) -> None:
        """Initialize CAN bus connection with retry logic."""
        logger.info(
            f"Starting CAN bus: interface={self.config['interface']}, "
            f"channel={self.config['channel']}"
        )

        # Create CAN bus with retry logic
        bus_config = {
            "interface": self.config["interface"],
            "channel": self.config["channel"],
            "receive_own_messages": True,  # Allow receiving own transmitted messages
        }

        # Add bitrate for interfaces that support it (not virtual)
        if self.config["interface"] != "virtual":
            bus_config["bitrate"] = self.config.get("bitrate", 500000)

        # Add CAN-FD support if configured
        if self.config.get("fd_mode", False):
            bus_config["fd"] = True
            bus_config["data_bitrate"] = self.config.get("data_bitrate", 2000000)

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

        # Cancel all periodic tasks
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

        try:
            state = self.bus.state
            return state in (can.BusState.ACTIVE, can.BusState.PASSIVE)
        except Exception as e:
            logger.debug(f"Bus health check failed: {e}")
            return False

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

    async def _run_async(self) -> None:
        """Main async loop - receive and decode CAN messages with reconnection."""
        if not self.bus:
            logger.error("Bus not initialized, call start() first")
            return

        reader = can.AsyncBufferedReader()
        notifier = can.Notifier(self.bus, [reader])

        try:
            logger.info("Starting async CAN message reception")
            while self.running:
                try:
                    # Wait for message with timeout to check bus health
                    msg = await asyncio.wait_for(reader.get_message(), timeout=5.0)
                    self._handle_message(msg)
                    self.last_message_time = time.time()
                except TimeoutError:
                    # Check if bus is still healthy
                    if not self._check_bus_health():
                        logger.error("Bus health check failed")
                        notifier.stop()
                        if await self._reconnect_bus():
                            # Recreate reader and notifier
                            reader = can.AsyncBufferedReader()
                            notifier = can.Notifier(self.bus, [reader])
                        else:
                            logger.error("Failed to reconnect, stopping")
                            break
                except can.CanError as e:
                    logger.error(f"CAN bus error: {e}, attempting reconnection")
                    notifier.stop()
                    if await self._reconnect_bus():
                        reader = can.AsyncBufferedReader()
                        notifier = can.Notifier(self.bus, [reader])
                    else:
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

        :param msg: Received CAN message
        """
        # Update metrics
        self.metrics["messages_received"] += 1
        self.metrics["bytes_received"] += len(msg.data)

        # Validate message
        max_dlc = 64 if self.config.get("fd_mode", False) else 8
        if msg.dlc > max_dlc:
            logger.warning(
                f"Invalid DLC {msg.dlc} for message {msg.arbitration_id:04x} (max: {max_dlc})"
            )
            return

        try:
            # Decode message using DBC
            decoded = self.db.decode_message(msg.arbitration_id, msg.data)

            # Get message definition
            dbc_msg = self.messages_by_id.get(msg.arbitration_id)
            if not dbc_msg:
                logger.debug(f"Unknown message ID: {msg.arbitration_id:04x}")
                self.metrics["unknown_messages"] += 1
                return

            # Convert values to native Python types (handle NamedSignalValue)
            signals = {}
            for signal_name, value in decoded.items():
                if isinstance(value, (int, float)):
                    signals[signal_name] = value
                else:
                    # Convert NamedSignalValue to numeric
                    signal_def = dbc_msg.get_signal_by_name(signal_name)
                    signals[signal_name] = signal_def.conversion.choice_to_number(value)

            # Emit trace event
            event_name = self._get_event_name(dbc_msg)
            getattr(self.source, event_name).log(**signals)
            logger.info(f"Decoded {event_name}: {signals}")

            self.metrics["messages_decoded"] += 1

        except KeyError:
            logger.debug(f"Message ID {msg.arbitration_id:04x} not in DBC")
            self.metrics["unknown_messages"] += 1
        except cantools.database.DecodeError as e:
            logger.debug(f"Decode error for {msg.arbitration_id:04x}: {e}")
            self.metrics["decode_errors"] += 1
        except Exception as e:
            logger.error(f"Error decoding message {msg.arbitration_id:04x}: {e}")
            self.metrics["decode_errors"] += 1

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
