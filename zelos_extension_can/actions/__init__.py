"""Cross-bus action surface registered under the `can` namespace.

The per-codec `<bus>/...` actions on `CanCodec` predate the
`actions.*` bridge in `web/app-extension-sdk` and stay registered for the
existing desktop actions panel. This package adds the new `can/...` surface
that the CAN Transmit app extension (and any future cross-bus consumer) speaks.

See: features/CAN_TRANSMIT.md §5 Block D.
"""

from .router import CanActionsRouter

__all__ = ["CanActionsRouter"]
