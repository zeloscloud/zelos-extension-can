"""Zelos CAN

A Zelos extension for CAN monitoring.
"""

from zelos_can.codec import CanCodec

#: Must equal `name` in extension.toml: the app and the packager address actions by it.
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
