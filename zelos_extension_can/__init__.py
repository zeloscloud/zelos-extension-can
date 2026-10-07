"""Zelos CAN

A Zelos extension for CAN monitoring.
"""

from zelos_can.codec import CanCodec

#: Action namespace for this extension. Single source for both surfaces — the
#: live registration (`zelos_sdk.init(name=ACTION_PREFIX)`) and the at-rest
#: inventory the packaging step dumps from `main.py`, which re-exports this.
#: A mismatch would silently produce two unrelated action trees.
#:
#: Matches `name` in `extension.toml`, which is what a user sees in the
#: extension list, so the address they read there is the address they type.
ACTION_PREFIX = "CAN"

#: A bus's `interface` as configured (the label the form shows) -> the
#: python-can interface it opens. `demo` and `other` are zelos-can's.
INTERFACES = {
    "SocketCAN": "zelos-socketcan",
    "SocketCAN over SSH": "zelos-ssh-socketcan",
    "PCAN": "pcan",
    "Kvaser": "kvaser",
    "Vector": "vector",
    "slcan (serial)": "slcan",
    "gs_usb (USB)": "gs_usb",
    "Other (python-can)": "other",
    "Demo": "demo",
}

__all__: list[str] = [
    "CanCodec",
    "ACTION_PREFIX",
    "INTERFACES",
]
