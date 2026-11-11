"""CSS Electronics CANsub (CANsub.2, CANsub.4) through python-can-cansub.

Needs the `cansub` extra. The device is reached over HTTPS and a websocket, by
its USB network adapter or Ethernet; no driver.
"""

import importlib.util
import json
import re
import warnings
from typing import Any

#: The python-can interface name, as `INTERFACES` maps the form's label.
INTERFACE = "cansub"

MISSING_EXTRA = (
    "CANsub needs this extension's cansub extra (python-can-cansub), "
    "which is not installed. Stop the extension, then add it with Change what's installed… "
    "on the extension's page, or run: "
    "zelos extensions reinstall zeloscloud.zelos-extension-can --extras cansub"
)

#: Seconds auto-configure listens for mDNS answers.
DISCOVERY_SECONDS = 2.0

#: The hostname on the device label: `<device id>-usb.local` or `<device id>-eth.local`.
_HOSTNAME = re.compile(r"^([0-9a-z]+)-(usb|eth)\.local\.?$", re.IGNORECASE)


class CansubConfigError(ValueError):
    """A CANsub bus entry cannot be used; the message is one line for the user."""


def _device_id(address: str) -> str:
    """The device id in a label hostname, else the address with every other char as '_'."""
    if match := _HOSTNAME.match(address):
        return match.group(1).lower()
    return re.sub(r"[^0-9A-Za-z]", "_", address)


def bus_config(bus: dict[str, Any]) -> dict[str, Any]:
    """A CANsub bus entry as python-can CanSub kwargs.

    The channel is `<address>@<n>`. The default name is the same over USB and
    Ethernet, so a trace keeps one source name. Listen only, the clock and the
    shutdown bound ride in `config_json`, under the user's own keys.

    :raises CansubConfigError: no address
    """
    config = dict(bus)
    address = config.pop("address", None)
    if not address:
        raise CansubConfigError("CANsub: set Address")
    # The vendor finds no bit timing for this and raises a bare ValueError.
    if config.get("fd_mode") and config.get("data_bitrate", 0) < config.get("bitrate", 0):
        raise CansubConfigError("CANsub: CAN-FD Data Bitrate must be at least the Bitrate")
    channel = config.pop("channel", 1)
    config["channel"] = f"{address}@{channel}"
    config["name"] = config.get("name") or f"cansub_{_device_id(address)}_ch{channel}"
    # The vendor flushes queued TX for up to 3 s on close; a device that never
    # confirms the close still takes shutdown_timeout + 5 s (vendor force-close).
    kwargs: dict[str, Any] = {"shutdown_timeout": 0.5}
    if config.pop("listen_only", False):
        kwargs["listen_only"] = True
    # Off: the vendor sets a clock skewed by over 1 s to host time, turning off PTP.
    if config.pop("ptp_synced", False):
        kwargs["max_device_time_skew"] = None
    extra = json.loads(config["config_json"]) if config.get("config_json") else {}
    config["config_json"] = json.dumps({**kwargs, **extra})
    return config


def require_extra() -> None:
    """:raises CansubConfigError: the cansub extra is not installed"""
    try:
        import python_can_cansub  # noqa: F401
    except ImportError as e:
        raise CansubConfigError(MISSING_EXTRA) from e


# ─── Discovery (auto-configure) ──────────────────────────────────────────────


def extra_installed() -> bool:
    """The cansub extra is importable, without importing it."""
    return importlib.util.find_spec("python_can_cansub") is not None


def discover() -> tuple[list[dict[str, Any]], str | None]:
    """CANsub channels advertised over mDNS, for auto-configure. Never raises.

    One entry per (device, channel): a device reached over both USB and
    Ethernet is listed once, by its USB hostname, so re-runs keep one address.

    :return: ({interface, address, channel, name} per channel, one sentence when
        the scan was skipped or a device was left out)
    """
    if not extra_installed():
        return [], "CANsub scan skipped: the cansub extra is not installed."
    try:
        import ifaddr
        from python_can_cansub import CanSub

        # Every IPv4 interface but loopback: another mDNS responder holding
        # 127.0.0.1:5353 fails the vendor's all-interfaces bind.
        addresses = [
            ip.ip
            for adapter in ifaddr.get_adapters()
            for ip in adapter.ips
            if ip.is_IPv4 and not ip.ip.startswith("127.")
        ]
        if not addresses:
            return [], None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            configs = CanSub.mdns_discover(addresses, discovery_time=DISCOVERY_SECONDS)
        notes = [
            f"{w.message}."
            for w in caught
            if issubclass(w.category, UserWarning) and "API version" in str(w.message)
        ]
        found, seen = [], set()
        for config in sorted(configs, key=lambda c: "-eth." in str(c["channel"])):
            address, _, n = str(config["channel"]).rpartition("@")
            key = (_device_id(address), int(n))
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "interface": INTERFACE,
                    "address": address,
                    "channel": key[1],
                    "name": f"CANsub {key[0]} ch{key[1]}",
                }
            )
    except Exception as e:
        return [], f"CANsub scan skipped: {e}."
    if notes:
        notes.append("Update the extension or the device firmware.")
    return found, " ".join(notes) or None
