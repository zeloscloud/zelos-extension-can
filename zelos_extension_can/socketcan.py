"""SocketCAN interfaces as the kernel reports them."""

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .health import HEALTH_DETAIL

# `ip -details` names for SocketCAN's controller states.
_KERNEL_CAN_STATES = {
    "ERROR-ACTIVE": "ok",
    "ERROR-WARNING": "warning",
    "ERROR-PASSIVE": "passive",
    "BUS-OFF": "bus_off",
}

# `ip -details` control-mode names, and the `ip link set ... type can` option
# that sets each. TDC modes are left to the kernel's default.
_CAN_CTRLMODE_OPTIONS = {
    "LOOPBACK": "loopback",
    "LISTEN-ONLY": "listen-only",
    "TRIPLE-SAMPLING": "triple-sampling",
    "ONE-SHOT": "one-shot",
    "BERR-REPORTING": "berr-reporting",
    "FD": "fd",
    "NON-ISO": "fd-non-iso",
    "PRESUME-ACK": "presume-ack",
    "CC-LEN8-DLC": "cc-len8-dlc",
}


@dataclass
class SocketcanLink:
    """What the kernel reports about one SocketCAN interface."""

    exists: bool
    # Administratively up. A bus-off controller loses carrier but stays up.
    up: bool = False
    # Health state, or None when the interface reports no CAN state (vcan).
    state: str | None = None
    counters: dict[str, int] | None = None
    restart_ms: int | None = None
    # `ip link set ... type can` arguments that reproduce its configuration;
    # empty when it has no bit timing, as a freshly plugged adapter has none.
    settings: list[str] = field(default_factory=list)


def socketcan_link(iface: str) -> SocketcanLink | None:
    """The kernel's view of `iface`, from `ip -details -json`; None when `ip`
    is missing or answers something unreadable."""
    try:
        r = subprocess.run(
            ["ip", "-details", "-json", "link", "show", "dev", iface],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return SocketcanLink(exists=False) if "does not exist" in r.stderr else None
    try:
        link = json.loads(r.stdout)[0]
    except Exception:
        return None
    info = (link.get("linkinfo") or {}).get("info_data") or {}
    counters = info.get("berr_counter") or {}
    return SocketcanLink(
        exists=True,
        up="UP" in (link.get("flags") or []),
        state=_KERNEL_CAN_STATES.get(info.get("state")),
        counters={
            name: counters[key]
            for name, key in (("tx_error_count", "tx"), ("rx_error_count", "rx"))
            if isinstance(counters.get(key), int)
        },
        restart_ms=info.get("restart_ms"),
        settings=socketcan_settings(info),
    )


def socketcan_settings(info: dict[str, Any]) -> list[str]:
    """The `ip link set ... type can` arguments for a controller's bit timing
    (by bitrate and sample point), control modes, restart delay and
    termination, from its `ip -details -json` info."""
    nominal = info.get("bittiming") or {}
    if not nominal.get("bitrate"):
        return []
    args = ["bitrate", str(nominal["bitrate"])]
    if nominal.get("sample_point"):
        args += ["sample-point", str(nominal["sample_point"])]
    data = info.get("data_bittiming") or {}
    if data.get("bitrate"):
        args += ["dbitrate", str(data["bitrate"])]
        if data.get("sample_point"):
            args += ["dsample-point", str(data["sample_point"])]
    for mode in info.get("ctrlmode") or []:
        if mode in _CAN_CTRLMODE_OPTIONS:
            args += [_CAN_CTRLMODE_OPTIONS[mode], "on"]
    if info.get("restart_ms"):
        args += ["restart-ms", str(info["restart_ms"])]
    if info.get("termination"):
        args += ["termination", str(info["termination"])]
    return args


def socketcan_health(
    iface: str, link: SocketcanLink, settings: list[str]
) -> tuple[str | None, str | None]:
    """The health state and reason for one SocketCAN interface, naming the
    command that fixes it where one does."""
    if not link.exists:
        return "unavailable", f"{iface} is gone. Check its USB connection."
    if not link.up:
        config = " ".join(settings) or "bitrate BITRATE"
        return "unavailable", (
            f"{iface} is down. Bring it up: sudo ip link set {iface} up type can {config}"
        )
    if link.state == "bus_off" and not link.restart_ms:
        return "bus_off", (
            f"{HEALTH_DETAIL['bus_off']} Restart it with sudo ip link set {iface} type can "
            f"restart, or let the kernel do it: sudo ip link set {iface} type can restart-ms 100"
        )
    return link.state, HEALTH_DETAIL.get(link.state) if link.state else None
