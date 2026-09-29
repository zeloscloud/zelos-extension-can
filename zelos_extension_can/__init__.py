"""Zelos CAN

A Zelos extension for CAN monitoring, importable as a library.

Importing the package has no side effects: no action is registered, and
`CanCodec` (which pulls in `cantools`) loads on first access.
"""

from typing import TYPE_CHECKING, Any

#: Action namespace for this extension. Single source for both surfaces — the
#: live registration (`zelos_sdk.init(name=ACTION_PREFIX)`) and the at-rest
#: inventory the packaging step dumps from `main.py`, which re-exports this.
#: A mismatch would silently produce two unrelated action trees.
#:
#: Matches `name` in `extension.toml`, which is what a user sees in the
#: extension list, so the address they read there is the address they type.
ACTION_PREFIX = "CAN"

if TYPE_CHECKING:
    from zelos_extension_can.codec import CanCodec

__all__: list[str] = [
    "CanCodec",
    "ACTION_PREFIX",
]


def __getattr__(name: str) -> Any:
    # Lazy so `zelos_extension_can.bus` imports without cantools.
    if name == "CanCodec":
        from zelos_extension_can.codec import CanCodec

        return CanCodec
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
