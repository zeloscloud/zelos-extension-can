"""J1939 protocol actions — registered only when J1939 is enabled on a bus.

These are free-standing functions with dynamic bus selectors so users
pick the target bus from a dropdown populated at runtime.
"""

from typing import Any

from zelos_sdk.actions import action

from .registry import get_codec, j1939_buses


def _get_j1939_handler(bus: str):
    """Resolve the J1939 handler for a bus, or return an error dict."""
    from ..protocols.j1939.handler import J1939Handler

    codec = get_codec(bus)
    if not codec:
        return None, {"error": f"Bus '{bus}' not found"}
    handler = codec._protocol_handler
    if not isinstance(handler, J1939Handler):
        return None, {"error": f"J1939 not active on bus '{bus}'"}
    return handler, None


@action("J1939 Address Table", "View discovered J1939 source addresses and PGN counts")
@action.select("bus", choices=j1939_buses, title="Bus")
def j1939_address_table(bus: str) -> dict[str, Any]:
    """Show tracked source addresses with PGN counts for a J1939 bus."""
    handler, err = _get_j1939_handler(bus)
    if err:
        return err
    return handler.get_address_table()


@action("J1939 TP Sessions", "View transport protocol session stats")
@action.select("bus", choices=j1939_buses, title="Bus")
def j1939_tp_sessions(bus: str) -> dict[str, Any]:
    """Show transport protocol (BAM / RTS-CTS) session statistics."""
    handler, err = _get_j1939_handler(bus)
    if err:
        return err
    return handler.get_tp_sessions()


@action("J1939 Diagnostics", "View active DM1/DM2 diagnostic trouble codes")
@action.select("bus", choices=j1939_buses, title="Bus")
def j1939_diagnostics(bus: str) -> dict[str, Any]:
    """Show decoded diagnostic trouble codes per source address."""
    handler, err = _get_j1939_handler(bus)
    if err:
        return err
    return handler.get_diagnostics()
