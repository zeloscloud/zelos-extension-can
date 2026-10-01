"""SocketCAN interfaces as the kernel reports them, and recovering one the
kernel will not recover on its own."""

import json
import logging
import math
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .health import HEALTH_DETAIL

logger = logging.getLogger(__name__)

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
    # The kernel's index for this instance: a replugged adapter gets a new one.
    ifindex: int | None = None
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
        ifindex=link.get("ifindex"),
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


def can_admin_links(status_file: Path = Path("/proc/self/status")) -> bool:
    """Whether this process may reconfigure network interfaces: it holds
    CAP_NET_ADMIN, as root does. Always False off Linux."""
    try:
        with status_file.open() as status:
            for line in status:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) >> 12 & 1)
    except (OSError, ValueError, IndexError):
        pass
    return False


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


class SocketcanSupervisor:
    """Recovers one SocketCAN interface the kernel will not recover on its own.

    A bus-off controller stays off unless `restart-ms` is set. An adapter
    unplugged and plugged back is a new interface: it comes back down, with
    none of its settings, and sockets bound to the old one hear nothing.
    Changing a link takes CAP_NET_ADMIN; without it `socketcan_health` names
    the command. An interface taken down or reconfigured by hand is never
    changed."""

    def __init__(self, iface: str, bus_name: str) -> None:
        self.iface = iface
        self.bus_name = bus_name
        # The interface the bus's sockets are bound to, and the settings it
        # last ran with, to restore on a replugged one: the kernel forgets
        # them with the device.
        self._ifindex: int | None = None
        self.settings: list[str] = []
        # Bus-off restarts since one was last logged, and when that was: a
        # shorted bus is restarted every look but logged once a minute.
        self._bus_off_restarts = 0
        self._bus_off_logged_at = -math.inf
        # The last failed link change logged, and when, so one that keeps
        # failing the same way is logged once a minute.
        self._link_failure_logged: tuple[str, float] | None = None

    def note_link(self) -> None:
        """Record the interface just opened, and its settings while it has them."""
        link = socketcan_link(self.iface)
        if link is not None and link.exists:
            self._ifindex = link.ifindex
            if link.settings:
                self.settings = link.settings

    def look(self) -> bool:
        """Restart a bus-off controller, and bring a replugged interface back
        up with its last settings.

        :return: True when the interface was replaced and is up, so the caller
            reopens the sockets bound to the old one.
        """
        link = socketcan_link(self.iface)
        if link is None or not link.exists:
            return False
        if self._ifindex is None:
            self._ifindex = link.ifindex
        replaced = link.ifindex != self._ifindex
        if not replaced and link.up and link.settings:
            self.settings = link.settings
        if not can_admin_links():
            return replaced and link.up
        settings = self.settings
        if replaced and not link.up and not link.settings and settings:
            return self._set_link(
                ["up", "type", "can", *settings],
                f"brought replugged {self.iface} back up: {' '.join(settings)}",
            )
        if link.state == "bus_off" and not link.restart_ms:
            self._restart_bus_off()
        return replaced and link.up

    def _restart_bus_off(self) -> None:
        if not self._set_link(["type", "can", "restart"]):
            return
        self._bus_off_restarts += 1
        now = time.monotonic()
        if now - self._bus_off_logged_at >= 60.0:
            times = self._bus_off_restarts
            again = f" ({times} restarts since the last report)" if times > 1 else ""
            logger.info("[%s] restarted %s after bus-off%s", self.bus_name, self.iface, again)
            self._bus_off_logged_at, self._bus_off_restarts = now, 0

    def _set_link(self, args: list[str], done: str | None = None) -> bool:
        try:
            r = subprocess.run(
                ["ip", "link", "set", "dev", self.iface, *args],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError) as e:
            self._warn_link_failure(str(e))
            return False
        if r.returncode != 0:
            self._warn_link_failure(r.stderr.strip())
            return False
        if done:
            logger.info("[%s] %s", self.bus_name, done)
        return True

    def _warn_link_failure(self, reason: str) -> None:
        now = time.monotonic()
        last = self._link_failure_logged
        if last is None or last[0] != reason or now - last[1] >= 60.0:
            logger.warning("[%s] Could not recover %s: %s", self.bus_name, self.iface, reason)
            self._link_failure_logged = (reason, now)
