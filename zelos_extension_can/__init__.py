"""Zelos CAN

A Zelos extension for CAN monitoring.
"""

from zelos_extension_can.codec import CanCodec

#: Action namespace for this extension. Single source for both surfaces — the
#: live registration (`zelos_sdk.init(name=ACTION_PREFIX)`) and the at-rest
#: inventory the packaging step dumps from `main.py`, which re-exports this.
#: A mismatch would silently produce two unrelated action trees.
ACTION_PREFIX = "can"

__all__: list[str] = [
    "CanCodec",
    "ACTION_PREFIX",
]
