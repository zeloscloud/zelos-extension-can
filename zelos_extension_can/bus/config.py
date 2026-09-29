"""Bus configuration preparation: raw `buses[]` entry -> codec config."""

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Bus-level settings and their defaults. The app offers them globally under
#: `advanced`; a legacy per-bus value still wins (see `prepare_bus_config`).
BUS_DEFAULTS: dict[str, Any] = {
    "log_raw_frames": True,
    "receive_own_messages": True,
    "emit_schemas_on_init": False,
    "timestamp_mode": "auto",
}


class BusConfigError(ValueError):
    """A bus configuration that cannot be used as given."""


def bus_database_files(bus_config: dict[str, Any]) -> list[str]:
    """A bus config's DBC list, in precedence order.

    A pre-list config carries one `database_file`; it takes precedence, so it
    is prepended to any list.
    """
    files = [str(p) for p in (bus_config.get("database_files") or [])]
    legacy = bus_config.get("database_file")
    return [str(legacy), *files] if legacy else files


def prepare_bus_config(
    bus_config: dict, demo_dbc_path: Path, advanced: Mapping[str, Any] | None = None
) -> dict:
    """Prepare a bus configuration, handling demo, 'other' and ssh-socketcan.

    Folds a legacy single `database_file` into the `database_files` list and
    applies the bus-level settings in `advanced`.

    :param bus_config: Raw bus configuration from the buses array
    :param demo_dbc_path: Path to demo DBC file
    :param advanced: Bus-level settings (`BUS_DEFAULTS` when omitted); extra keys are ignored
    :return: Prepared configuration dict
    :raises BusConfigError: the configuration cannot be used
    """
    config = bus_config.copy()
    bus_name = config.get("name", "bus")
    advanced = advanced if advanced is not None else BUS_DEFAULTS

    config["database_files"] = bus_database_files(config)
    config.pop("database_file", None)

    # Global advanced settings; a legacy per-bus value overrides.
    for key in BUS_DEFAULTS:
        config.setdefault(key, advanced[key])

    # Handle demo interface selection
    if config.get("interface") == "demo":
        logger.info(f"[{bus_name}] Demo mode: using built-in EV simulator")
        config["demo_mode"] = True
        config["interface"] = "virtual"
        config["channel"] = "vcan0"
        config["database_files"] = [str(demo_dbc_path)]
        config["receive_own_messages"] = True

    # Handle "other" interface - merge config_json into main config
    if config.get("interface") == "other":
        logger.info(f"[{bus_name}] Using custom interface from config_json")

        if "config_json" not in config or not config["config_json"]:
            raise BusConfigError(
                f"[{bus_name}] 'other' interface requires config_json with interface and channel"
            )
        try:
            custom_config = json.loads(config["config_json"])
            if "interface" not in custom_config:
                raise BusConfigError(f"[{bus_name}] config_json must include 'interface' key")
            if "channel" not in custom_config:
                raise BusConfigError(f"[{bus_name}] config_json must include 'channel' key")
            # Merge custom config into main config
            config["interface"] = custom_config.pop("interface")
            config["channel"] = custom_config.pop("channel")
            # Update config_json with remaining custom parameters
            config["config_json"] = json.dumps(custom_config) if custom_config else ""
            logger.info(
                f"[{bus_name}] Custom interface: {config['interface']}, "
                f"channel: {config['channel']}"
            )
        except json.JSONDecodeError as e:
            raise BusConfigError(f"[{bus_name}] Invalid JSON in config_json: {e}") from e

    # Handle ssh-socketcan - synthesize the "[user@]host:iface" channel the
    # transport parses from the user-facing remote_host / ssh_user /
    # remote_channel fields.
    if config.get("interface") == "ssh-socketcan":
        host = config.get("remote_host")
        if not host:
            raise BusConfigError(f"[{bus_name}] 'ssh-socketcan' interface requires 'remote_host'")
        user = config.get("ssh_user")
        iface = config.get("remote_channel", "can0")
        config["channel"] = f"{user}@{host}:{iface}" if user else f"{host}:{iface}"
        logger.info(f"[{bus_name}] ssh-socketcan channel: {config['channel']}")

    return config
