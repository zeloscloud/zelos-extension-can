"""Protocol handler factory for CAN bus protocols."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import ProtocolHandler

if TYPE_CHECKING:
    import zelos_sdk


def create_handler(
    config: dict[str, Any],
    source: zelos_sdk.TraceSource,
    namespace: zelos_sdk.TraceNamespace | None,
    bus_name: str | None,
) -> ProtocolHandler | None:
    """Create a protocol handler based on config.

    :return: Protocol handler instance, or None when no protocol selected
    """
    if config.get("j1939_enabled"):
        from .j1939.handler import J1939Handler

        return J1939Handler(config, source, namespace, bus_name)

    if config.get("canopen"):
        from .canopen.handler import CANopenHandler

        return CANopenHandler(config, source, namespace, bus_name)

    return None


__all__ = ["ProtocolHandler", "create_handler"]
