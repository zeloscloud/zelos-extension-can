"""Cross-bus action surface for the CAN extension.

The per-codec `<bus>/...` actions on `CanCodec` predate the cross-bus
`actions.*` bridge in the Zelos host app and stay registered for the existing
desktop actions panel. This package adds the new `can/tx/...` surface that the
CAN Transmit marketplace app (and any future cross-bus consumer) speaks.
"""

from .router import CanActionsRouter

__all__ = ["CanActionsRouter"]
