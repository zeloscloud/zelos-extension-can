"""Global codec registry and dynamic dropdown helpers for actions.

Codecs are registered here after creation so that free-standing ``@action``
functions can reference them via dynamic ``choices`` callbacks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..codec import CanCodec

# Global registry: action_name → CanCodec instance
_codecs: dict[str, CanCodec] = {}


def register(name: str, codec: CanCodec) -> None:
    """Register a codec for use by action dynamic dropdowns."""
    _codecs[name] = codec


def get_codec(name: str) -> CanCodec | None:
    """Look up a registered codec by name."""
    return _codecs.get(name)


# --- Dynamic dropdown callbacks (passed as choices=callable) ---


def j1939_buses() -> list[str]:
    """Return names of buses with J1939 protocol enabled."""
    from ..protocols.j1939.handler import J1939Handler

    return [
        name for name, codec in _codecs.items() if isinstance(codec._protocol_handler, J1939Handler)
    ]


def all_buses() -> list[str]:
    """Return names of all registered buses."""
    return list(_codecs.keys())


def bus_messages(bus: str) -> list[str]:
    """Return human-readable DBC message names for a given bus."""
    codec = _codecs.get(bus)
    if not codec:
        return []
    return sorted(codec.messages_by_name.keys())


def bus_signals(bus: str, message: str) -> list[str]:
    """Return signal names for a specific message on a given bus."""
    codec = _codecs.get(bus)
    if not codec:
        return []
    dbc_msg = codec.messages_by_name.get(message)
    if not dbc_msg:
        return []
    return sorted(sig.name for sig in dbc_msg.signals)
