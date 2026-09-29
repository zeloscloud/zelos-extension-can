"""python-can bus construction from a prepared bus config."""

import json
import logging
import sys
import time
from typing import Any

import can

logger = logging.getLogger(__name__)


def open_python_can_bus(config: dict[str, Any], bus_name: str) -> can.BusABC:
    """Open a python-can bus for a prepared bus config, retrying init up to 3 times.

    Not for `socketcan` or `ssh-socketcan`: `CanCodec` runs those on `zelos_can`.

    :param config: Prepared bus config (see `prepare_bus_config`)
    :param bus_name: Bus name for log lines
    :raises can.CanError: the last init attempt failed
    :raises ValueError: `config_json` is not valid JSON
    """
    # `socketcan-py` is python-can's own `socketcan`.
    interface = config["interface"]
    bus_config = {
        "interface": "socketcan" if interface == "socketcan-py" else interface,
        "channel": config["channel"],
    }

    # Pass through optional bus config parameters if specified
    if "receive_own_messages" in config:
        bus_config["receive_own_messages"] = config["receive_own_messages"]

    if "bitrate" in config:
        bus_config["bitrate"] = config["bitrate"]

    if config.get("fd_mode", False):
        bus_config["fd"] = True
        if "data_bitrate" in config:
            bus_config["data_bitrate"] = config["data_bitrate"]

    # Merge additional config_json (advanced interface-specific options)
    if "config_json" in config and config["config_json"]:
        try:
            additional_config = json.loads(config["config_json"])
            logger.info("Merging additional config: %s", list(additional_config.keys()))
            bus_config.update(additional_config)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse config_json: %s", e)
            raise ValueError(f"Invalid config_json: {e}") from e

    # PCAN's macOS library (PCBUSB) cannot echo TX frames, and python-can
    # fails the whole init on it.
    if bus_config["interface"] == "pcan" and sys.platform == "darwin":
        if bus_config.pop("receive_own_messages", False):
            logger.warning(
                "[%s] PCAN on macOS cannot receive its own messages; ignoring "
                "receive_own_messages, so transmitted frames are not traced",
                bus_name,
            )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            bus = can.Bus(**bus_config)
            logger.info("CAN bus started successfully")
            return bus
        except can.CanError as e:
            if attempt == max_retries - 1:
                logger.error("Failed to initialize CAN bus after %d attempts", max_retries)
                raise
            logger.warning("Bus init failed (attempt %d/%d): %s", attempt + 1, max_retries, e)
            time.sleep(1)
