"""CAN message transmission actions (raw and DBC-encoded)."""

import asyncio
import json
import logging
from typing import Any

import can
from zelos_sdk.actions import action

from .registry import all_buses, bus_messages, get_codec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Raw transmit
# ---------------------------------------------------------------------------


@action("Send Message", "Send a single CAN message")
@action.select("bus", choices=all_buses, title="Bus")
@action.number("msg_id", minimum=0, maximum=0x1FFFFFFF, title="Message ID", default=0x100)
@action.text("data", title="Data (hex bytes)", placeholder="01 02 03 04", default="00")
@action.boolean("extended_id", title="Extended ID (29-bit)", default=False, widget="toggle")
def send_message(bus: str, msg_id: int, data: str, extended_id: bool = False) -> dict[str, Any]:
    """Send a CAN message."""
    codec = get_codec(bus)
    if not codec:
        return {"error": f"Bus '{bus}' not found"}
    if not codec.bus:
        return {"error": "CAN bus not started"}

    max_id = 0x1FFFFFFF if extended_id else 0x7FF
    if msg_id > max_id:
        id_type = "extended" if extended_id else "standard"
        return {"error": f"Message ID {msg_id:x} exceeds max for {id_type} ID ({max_id:x})"}

    try:
        data_bytes = bytes.fromhex(data.replace(" ", ""))
        msg = can.Message(
            arbitration_id=msg_id,
            data=data_bytes,
            is_extended_id=extended_id,
            is_fd=codec.fd_mode,
        )
        codec.bus.send(msg)
        logger.info(
            "Sent message: ID=%04x, data=%s, extended=%s", msg_id, data_bytes.hex(), extended_id
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


# ---------------------------------------------------------------------------
# DBC-encoded transmit
# ---------------------------------------------------------------------------


@action("Transmit DBC Message", "Encode and transmit a DBC-defined CAN message")
@action.select("bus", choices=all_buses, title="Bus")
@action.select("message", choices=bus_messages, depends_on="bus", title="Message")
@action.text(
    "signal_values",
    title="Signal Values (JSON)",
    description='Map signal names to values, e.g. {"EngineSpeed": 1500, "VehicleSpeed": 60}',
    placeholder='{"SignalName": 0}',
)
def transmit_dbc_message(bus: str, message: str, signal_values: str) -> dict[str, Any]:
    """Encode signals via DBC definition and transmit the CAN message."""
    codec = get_codec(bus)
    if not codec:
        return {"error": f"Bus '{bus}' not found"}

    dbc_msg = codec.messages_by_name.get(message)
    if not dbc_msg:
        return {"error": f"Message '{message}' not found in database"}

    try:
        values = json.loads(signal_values)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

    if not codec.bus:
        return {"error": "CAN bus not started"}

    try:
        data = dbc_msg.encode(values)
    except Exception as e:
        return {"error": f"Encode failed: {e}"}

    try:
        msg = can.Message(
            arbitration_id=dbc_msg.frame_id,
            data=data,
            is_extended_id=dbc_msg.is_extended_frame,
            is_fd=codec.fd_mode,
        )
        codec.bus.send(msg)
        logger.info("Sent DBC message '%s': data=%s", message, data.hex())
        return {
            "status": "sent",
            "message": message,
            "id": f"0x{dbc_msg.frame_id:08x}",
            "data": data.hex(),
            "signals": values,
        }
    except can.CanError as e:
        logger.error("CAN error sending DBC message: %s", e)
        return {"error": f"CAN error: {e}"}
    except Exception as e:
        logger.error("Error sending DBC message: %s", e)
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Periodic transmit
# ---------------------------------------------------------------------------


@action("Start Periodic Message", "Start periodic transmission of a CAN message")
@action.select("bus", choices=all_buses, title="Bus")
@action.number("msg_id", minimum=0, maximum=0x1FFFFFFF, title="Message ID", default=0x100)
@action.text("data", title="Data (hex)", placeholder="01 02 03 04", default="00")
@action.number("period", minimum=0.001, maximum=10.0, title="Period (seconds)", default=0.1)
@action.boolean("extended_id", title="Extended ID (29-bit)", default=False, widget="toggle")
def start_periodic(
    bus: str, msg_id: int, data: str, period: float, extended_id: bool = False
) -> dict[str, Any]:
    """Start periodic transmission of a message."""
    codec = get_codec(bus)
    if not codec:
        return {"error": f"Bus '{bus}' not found"}
    if not codec.bus or not codec.running:
        return {"error": "CAN bus not running"}

    max_id = 0x1FFFFFFF if extended_id else 0x7FF
    if msg_id > max_id:
        id_type = "extended" if extended_id else "standard"
        return {"error": f"Message ID {msg_id:x} exceeds max for {id_type} ID ({max_id:x})"}

    try:
        data_bytes = bytes.fromhex(data.replace(" ", ""))
        task_name = f"periodic_{msg_id:08x}" if extended_id else f"periodic_{msg_id:04x}"

        if task_name in codec.periodic_tasks:
            codec.periodic_tasks[task_name].cancel()
            logger.info("Cancelled existing periodic task: %s", task_name)

        task = asyncio.create_task(
            codec._periodic_send_task(msg_id, data_bytes, period, task_name, extended_id)
        )
        codec.periodic_tasks[task_name] = task

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
@action.select("bus", choices=all_buses, title="Bus")
@action.text("task_name", title="Task Name", placeholder="periodic_0100")
def stop_periodic(bus: str, task_name: str) -> dict[str, Any]:
    """Stop periodic transmission of a message."""
    codec = get_codec(bus)
    if not codec:
        return {"error": f"Bus '{bus}' not found"}

    if task_name in codec.periodic_tasks:
        codec.periodic_tasks[task_name].cancel()
        del codec.periodic_tasks[task_name]
        logger.info("Stopped periodic task: %s", task_name)
        return {"status": "stopped", "task_name": task_name}
    return {"error": f"No periodic task found: {task_name}"}


@action("List Periodic Tasks", "Show all active periodic transmissions")
@action.select("bus", choices=all_buses, title="Bus")
def list_periodic_tasks(bus: str) -> dict[str, Any]:
    """List all active periodic transmission tasks."""
    codec = get_codec(bus)
    if not codec:
        return {"error": f"Bus '{bus}' not found"}

    tasks = [
        {"name": name, "running": not task.done(), "cancelled": task.cancelled()}
        for name, task in codec.periodic_tasks.items()
    ]
    return {"count": len(tasks), "tasks": tasks}
