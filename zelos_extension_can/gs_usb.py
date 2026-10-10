"""gs_usb (candleLight, CANable gs_usb firmware) through python-can's gs_usb backend.

Needs the `gs_usb` extra: gs-usb, pyusb and libusb-package, which bundles the
native libusb-1.0 that pip cannot install.
"""

import contextlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

#: The python-can interface name, as `INTERFACES` maps the form's label.
INTERFACE = "gs_usb"

MISSING_EXTRA = (
    "gs_usb (USB) needs this extension's gs_usb extra (gs-usb, libusb-package), "
    "which is not installed"
)


#: Hard deadline on the auto-configure scan.
SCAN_SECONDS = 3.0

#: An adapter whose product string cannot be read.
FALLBACK_NAME = "gs_usb adapter"

#: Linux's USB tree: which devices the kernel gs_usb driver holds.
_SYS_USB = Path("/sys/bus/usb")


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


# ─── Discovery (auto-configure) ──────────────────────────────────────────────
#
# The scan runs in a child process, killed at the deadline: libusb calls cannot
# be interrupted, and a thread stuck in one would sit in the live extension and
# race pyusb's at-exit libusb_exit in a standalone run. The child enumerates
# only; its one device access is a best-effort product string read.


def extra_installed() -> bool:
    """The gs_usb extra is importable, without importing it."""
    return all(importlib.util.find_spec(m) for m in ("gs_usb", "usb", "libusb_package"))


def kernel_bound() -> dict[tuple[int, int], list[str]]:
    """(bus, address) of each USB device the kernel gs_usb driver holds -> its netdevs.

    Read from sysfs, opening no device.

    :raises OSError: sysfs cannot be read
    :raises ValueError: a busnum/devnum is not a number
    """
    if not (_SYS_USB / "devices").is_dir():
        raise OSError(f"{_SYS_USB} is not readable")
    driver = _SYS_USB / "drivers" / "gs_usb"
    if not driver.is_dir():  # module not loaded: holds nothing
        return {}
    bound: dict[tuple[int, int], list[str]] = {}
    for link in driver.iterdir():
        if ":" not in link.name:  # bind, unbind, module, ...; interfaces are `1-1:1.0`
            continue
        iface = link.resolve()
        dev = iface.parent
        key = (int((dev / "busnum").read_text()), int((dev / "devnum").read_text()))
        net = iface / "net"
        bound.setdefault(key, []).extend(
            sorted(p.name for p in net.iterdir()) if net.is_dir() else []
        )
    return bound


def _scan_command(skip: list[tuple[int, int]]) -> list[str]:
    # -P: the script's directory holds this file, which would shadow the gs_usb package.
    return [sys.executable, "-P", __file__, json.dumps(skip)]


def scan(skip: list[tuple[int, int]], timeout: float) -> list[dict[str, Any]]:
    """Every gs_usb device in GsUsb.scan() order: bus, address, product name or None.

    :param skip: (bus, address) whose product string is not read
    :raises RuntimeError: no answer within `timeout`, or the scan failed
    """
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        _scan_command(skip),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=flags,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=0.5)
        raise RuntimeError(f"no answer within {timeout:g} s") from None
    if proc.returncode:
        lines = err.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"exit code {proc.returncode}")
    return [
        {"bus": int(d["bus"]), "address": int(d["address"]), "name": d.get("name")}
        for d in json.loads(out)
    ]


def discover() -> tuple[list[dict[str, Any]], dict[int, list[str]], str]:
    """gs_usb adapters for auto-configure. Never raises.

    :return: (adapters this interface can open, as {index, name}; on Linux the
        kernel-held ones as {index: netdevs}; one sentence when the scan was skipped)
    """
    if not extra_installed():
        return [], {}, ""
    try:
        bound = kernel_bound() if sys.platform == "linux" else {}
    except Exception:  # fail closed: no telling which are SocketCAN
        return [], {}, "gs_usb scan skipped: cannot read /sys/bus/usb."
    try:
        found = scan(sorted(bound), SCAN_SECONDS)
    except Exception as e:
        return [], {}, f"gs_usb scan skipped: {e}."
    adapters, held = [], {}
    # The index is the device's place in the full scan, kernel-held ones included,
    # as GsUsbBus counts it.
    for index, dev in enumerate(found):
        netdevs = bound.get((dev["bus"], dev["address"]))
        if netdevs is not None:
            held[index] = netdevs
        else:
            adapters.append({"index": index, "name": dev["name"] or FALLBACK_NAME})
    return adapters, held, ""


def _describe(devices: list[Any], skip: set[tuple[int, int]]) -> list[dict[str, Any]]:
    """GsUsb.scan() results as bus, address and product string, None when unreadable."""
    out = []
    for dev in devices:
        usb_dev = dev.gs_usb
        key = (usb_dev.bus, usb_dev.address)
        name = None
        if key not in skip:
            # Needs an open handle; Windows without WinUSB, or no permission, refuses it.
            with contextlib.suppress(Exception):
                name = usb_dev.product
        out.append({"bus": key[0], "address": key[1], "name": name})
    return out


def _scan_main() -> None:
    """Child process: print the scan as JSON."""
    import usb.util
    from gs_usb.gs_usb import GsUsb

    prime_libusb()
    devices = GsUsb.scan()
    try:
        skip = {tuple(k) for k in json.loads(sys.argv[1])}
        print(json.dumps(_describe(devices, skip)))
    finally:
        for dev in devices:
            with contextlib.suppress(Exception):
                usb.util.dispose_resources(dev.gs_usb)


if __name__ == "__main__":
    _scan_main()
