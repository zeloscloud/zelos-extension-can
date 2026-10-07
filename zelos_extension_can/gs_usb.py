"""gs_usb (candleLight, CANable gs_usb firmware) through python-can's gs_usb backend.

Needs the `gs_usb` extra: gs-usb, pyusb and libusb-package, which bundles the
native libusb-1.0 that pip cannot install.
"""

import json
from typing import Any

#: The python-can interface name, as `INTERFACES` maps the form's label.
INTERFACE = "gs_usb"

MISSING_EXTRA = (
    "gs_usb (USB) needs this extension's gs_usb extra (gs-usb, libusb-package), "
    "which is not installed"
)


class GsUsbConfigError(ValueError):
    """A gs_usb bus entry cannot be used; the message is one line for the user."""


def bus_config(bus: dict[str, Any]) -> dict[str, Any]:
    """A gs_usb bus entry as python-can GsUsbBus kwargs.

    The device is picked by index (`channel`, scan order, default 0) or by
    `usb_bus` and `usb_address`, never both, as python-can requires. bus and
    address ride in `config_json`, which the bus factory merges into Bus().

    :raises GsUsbConfigError: index together with bus/address, or only one of bus/address
    """
    config = dict(bus)
    usb_bus, usb_address = config.pop("usb_bus", None), config.pop("usb_address", None)
    if usb_bus is None and usb_address is None:
        config["channel"] = config.get("channel", 0)
        config["name"] = config.get("name") or f"gs_usb{config['channel']}"
        return config
    if "channel" in config:
        raise GsUsbConfigError(
            "gs_usb: set either Device Index or USB Bus and USB Address, not both"
        )
    if usb_bus is None or usb_address is None:
        raise GsUsbConfigError("gs_usb: USB Bus and USB Address must be set together")
    extra = json.loads(config["config_json"]) if config.get("config_json") else {}
    config["config_json"] = json.dumps({**extra, "bus": usb_bus, "address": usb_address})
    # GsUsbBus only reports `channel`; it opens the device by bus/address.
    config["channel"] = 0
    config["name"] = config.get("name") or f"gs_usb_{usb_bus}_{usb_address}"
    return config


def require_extra() -> None:
    """:raises GsUsbConfigError: the gs_usb extra is not installed"""
    try:
        import gs_usb  # noqa: F401
        import libusb_package  # noqa: F401
        import usb  # noqa: F401
    except ImportError as e:
        raise GsUsbConfigError(MISSING_EXTRA) from e


def prime_libusb() -> None:
    """Load libusb-package's bundled libusb-1.0 into pyusb before gs_usb opens a bus.

    pyusb caches the first libusb1 backend it loads (`_lib_object`), and gs_usb
    then calls `get_backend()` with no arguments, so this one call decides the
    library. Where libusb-package bundles none, its finder falls back to the
    system libusb. A no-op without libusb-package.
    """
    try:
        import libusb_package
        from usb.backend import libusb1
    except ImportError:
        return
    if libusb1.get_backend(find_library=libusb_package.find_library) is None:
        raise GsUsbConfigError("gs_usb: libusb-1.0 could not be loaded")
