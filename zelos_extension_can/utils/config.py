"""Configuration loading and validation."""

import json
import os
from pathlib import Path
from typing import Any


def validate_config(config: dict[str, Any]) -> list[str]:
    """Validate configuration and return list of errors.

    :param config: Configuration dictionary
    :return: List of validation error messages
    """
    errors = []

    # Check required fields
    if "interface" not in config:
        errors.append("Missing required field: interface")
    if "channel" not in config:
        errors.append("Missing required field: channel")
    if "dbc_file" not in config:
        errors.append("Missing required field: dbc_file")

    # Validate DBC file exists
    if "dbc_file" in config:
        dbc_path = config["dbc_file"]
        if not os.path.exists(dbc_path):
            errors.append(f"DBC file not found: {dbc_path}")
        elif not dbc_path.endswith((".dbc", ".DBC")):
            errors.append(f"DBC file must have .dbc extension: {dbc_path}")

    # Validate interface-specific requirements
    interface = config.get("interface", "")
    channel = config.get("channel", "")

    if interface == "socketcan" and not channel.startswith(("can", "vcan")):
        errors.append(
            f"socketcan interface requires channel like 'can0' or 'vcan0', got: {channel}"
        )

    if interface == "pcan" and not channel.startswith("PCAN"):
        errors.append(f"PCAN interface requires channel like 'PCAN_USBBUS1', got: {channel}")

    # Validate bitrate
    if "bitrate" in config:
        bitrate = config["bitrate"]
        valid_bitrates = [125000, 250000, 500000, 1000000]
        if bitrate not in valid_bitrates:
            errors.append(f"Invalid bitrate: {bitrate}. Must be one of {valid_bitrates}")

    # Validate CAN-FD data bitrate
    if config.get("fd_mode", False) and "data_bitrate" in config:
        data_bitrate = config["data_bitrate"]
        if data_bitrate < 500000 or data_bitrate > 8000000:
            errors.append(
                f"Invalid CAN-FD data bitrate: {data_bitrate}. Must be between 500000 and 8000000"
            )

    return errors


def load_config() -> dict[str, Any]:
    """Load configuration from file or schema defaults.

    Priority:
    1. config.json (written by Zelos from user settings)
    2. Defaults from config.schema.json
    3. Fallback to empty dict

    :return: Configuration dictionary
    """
    # First try config.json
    config_file = Path("config.json")
    if config_file.exists():
        with open(config_file) as f:
            return json.load(f)

    # Fall back to defaults from schema
    schema_file = Path("config.schema.json")
    if schema_file.exists():
        with open(schema_file) as f:
            schema = json.load(f)
            # Extract defaults from schema properties
            config: dict[str, Any] = {}
            for key, prop in schema.get("properties", {}).items():
                if "default" in prop:
                    config[key] = prop["default"]
            return config

    # Last resort fallback
    return {}
