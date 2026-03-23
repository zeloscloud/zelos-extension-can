"""Read-only status and informational actions."""

import time
from typing import Any

from zelos_sdk.actions import action

from .registry import all_buses, get_codec


@action("Get Status", "View CAN bus status")
@action.select("bus", choices=all_buses, title="Bus")
def get_status(bus: str) -> dict[str, Any]:
    """Get current CAN bus status."""
    codec = get_codec(bus)
    if not codec:
        return {"error": f"Bus '{bus}' not found"}

    bus_state = "not_initialized" if not codec.bus else str(codec.bus.state.name)
    return {
        "bus_state": bus_state,
        "running": codec.running,
        "interface": codec.config["interface"],
        "channel": codec.config["channel"],
        "fd_mode": codec.fd_mode,
    }


@action("Get Metrics", "View performance metrics and statistics")
@action.select("bus", choices=all_buses, title="Bus")
def get_metrics(bus: str) -> dict[str, Any]:
    """Get codec performance metrics."""
    codec = get_codec(bus)
    if not codec:
        return {"error": f"Bus '{bus}' not found"}

    uptime = time.time() - codec.start_time
    received = codec.metrics.messages_received
    return {
        "messages_received": received,
        "messages_decoded": codec.metrics.messages_decoded,
        "decode_errors": codec.metrics.decode_errors,
        "unknown_messages": codec.metrics.unknown_messages,
        "uptime_seconds": round(uptime, 2),
        "messages_per_second": round(received / max(uptime, 1), 2),
        "decode_success_rate": round(codec.metrics.messages_decoded / max(received, 1), 4),
    }


@action("List Messages", "List all messages in database")
@action.select("bus", choices=all_buses, title="Bus")
def list_messages(bus: str) -> dict[str, Any]:
    """List all CAN messages in the loaded database file."""
    codec = get_codec(bus)
    if not codec:
        return {"error": f"Bus '{bus}' not found"}

    messages = [
        {
            "id": f"0x{msg.frame_id:04x}",
            "name": msg.name,
            "length": msg.length,
            "signals": len(msg.signals),
        }
        for msg in codec.db.messages
    ]
    return {"count": len(messages), "messages": messages}
