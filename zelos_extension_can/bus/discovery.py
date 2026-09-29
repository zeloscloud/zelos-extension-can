"""Local SocketCAN interface discovery. Reads sysfs only: no privileges, no sockets."""

import sys
from pathlib import Path

#: Where Linux publishes its network interfaces. A CAN interface is a netdev
#: like any other, told apart by its ARPHRD type.
SYS_CLASS_NET = Path("/sys/class/net")
_ARPHRD_CAN = "280"


def _read_sysfs(path: Path) -> str:
    """One sysfs attribute, or "" when it is absent or unreadable."""
    try:
        return path.read_text().strip()
    except OSError:  # raced away mid-scan, or not readable: just unknown
        return ""


def _is_virtual(name: str) -> bool:
    """A kernel vcan interface: usable, but with no CAN hardware behind it."""
    return name.startswith("vcan")


def _interface_rank(iface: dict[str, str]) -> tuple[int, str]:
    """Real CAN devices first, virtual ones after; names break ties."""
    return (1 if _is_virtual(iface["name"]) else 0, iface["name"])


def interface_choice(iface: dict[str, str]) -> dict[str, str]:
    """One `choices` entry: the name, and what a person needs to pick it.

    The app's picker renders `detail` as dim right-aligned text beside the
    value, so `detail` carries the notes alone: `up, gs_usb` / `virtual`.

    `unknown` is left out: vcan reports it and is perfectly usable, so it says
    nothing. `down` is kept: that bus needs `ip link set <if> up` first.
    """
    notes = [iface["state"]] if iface["state"] in ("up", "down") else []
    kind = iface["driver"] or ("virtual" if _is_virtual(iface["name"]) else "")
    if kind:
        notes.append(kind)
    return {"value": iface["name"], "detail": ", ".join(notes)}


def local_can_interfaces(sys_class_net: Path = SYS_CLASS_NET) -> list[dict[str, str]]:
    """This machine's SocketCAN interfaces: name, operstate, driver.

    SocketCAN is Linux-only, so macOS/Windows answer with an honest empty list
    rather than an error: there is nothing to enumerate there.
    """
    if sys.platform != "linux" or not sys_class_net.is_dir():
        return []
    found = []
    for entry in sys_class_net.iterdir():
        if _read_sysfs(entry / "type") != _ARPHRD_CAN:
            continue
        # A symlink into the driver owning the device (gs_usb, peak_usb, ...),
        # absent for a virtual interface, which has no device behind it.
        driver = entry / "device" / "driver"
        found.append(
            {
                "name": entry.name,
                "state": _read_sysfs(entry / "operstate") or "unknown",
                "driver": driver.resolve().name if driver.exists() else "",
            }
        )
    return sorted(found, key=_interface_rank)


def list_interfaces(sys_class_net: Path = SYS_CLASS_NET) -> list[dict[str, str]]:
    """Local SocketCAN interfaces as `action-choices` entries, in the order to show."""
    return [interface_choice(iface) for iface in local_can_interfaces(sys_class_net)]
