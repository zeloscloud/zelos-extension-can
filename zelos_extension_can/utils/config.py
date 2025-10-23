"""Configuration loading and validation."""

import base64
import json
from pathlib import Path
from typing import Any


def get_platform_defaults() -> dict[str, str]:
    """Get platform-specific default interface and channel.

    :return: Dictionary with 'interface' and 'channel' defaults
    """
    # Default to demo mode for all platforms
    return {"interface": "demo"}


def data_url_to_file(data_url: str, output_path: str) -> str:
    """Convert data-url (base64 encoded file) to a file on disk.

    :param data_url: Data URL in format "data:mime/type;base64,<encoded_data>"
    :param output_path: Path where to save the decoded file
    :return: Path to the saved file
    """
    if not data_url or not data_url.startswith("data:"):
        raise ValueError(f"Invalid data URL format: {data_url[:50]}...")

    # Split: "data:application/octet-stream;base64,<data>"
    try:
        header, encoded = data_url.split(",", 1)
    except ValueError as e:
        raise ValueError("Data URL missing comma separator") from e

    # Decode base64
    try:
        file_bytes = base64.b64decode(encoded)
    except Exception as e:
        raise ValueError(f"Failed to decode base64 data: {e}") from e

    # Write to file
    output_path_obj = Path(output_path)
    output_path_obj.parent.mkdir(parents=True, exist_ok=True)

    with Path.open(output_path_obj, "wb") as f:
        f.write(file_bytes)

    return str(output_path_obj)


def validate_config(config: dict[str, Any]) -> list[str]:
    """Validate configuration and return list of errors.

    :param config: Configuration dictionary
    :return: List of validation error messages
    """
    errors = []

    # Check demo mode first
    demo_mode = config.get("demo_mode", False)

    # In demo mode, interface/channel/dbc_file are not required
    if not demo_mode:
        # Check required fields for normal mode
        if "interface" not in config:
            errors.append("Missing required field: interface")
        if "channel" not in config:
            errors.append("Missing required field: channel")
        if "dbc_file" not in config:
            errors.append("Missing required field: dbc_file")

    # Validate DBC file (can be data-url or plain file path)
    if "dbc_file" in config:
        dbc_value = config["dbc_file"]

        # If it's a data-url (uploaded file), validate it's decodable
        if dbc_value.startswith("data:"):
            try:
                header, encoded = dbc_value.split(",", 1)
                base64.b64decode(encoded[:100])  # Just validate first 100 chars
            except Exception as e:
                errors.append(f"Invalid DBC file upload: {e}")
        else:
            # It's a plain file path - validate it exists
            if not Path(dbc_value).exists():
                errors.append(f"DBC file not found: {dbc_value}")
            elif not dbc_value.endswith((".dbc", ".DBC")):
                errors.append(f"DBC file must have .dbc extension: {dbc_value}")

    # Validate interface-specific requirements (skip in demo mode)
    if not demo_mode:
        interface = config.get("interface", "")
        channel = config.get("channel", "")

        if interface == "socketcan" and not channel.startswith(("can", "vcan")):
            errors.append(
                f"socketcan interface requires channel like 'can0' or 'vcan0', got: {channel}"
            )

        if interface == "pcan" and not channel.startswith("PCAN"):
            errors.append(f"PCAN interface requires channel like 'PCAN_USBBUS1', got: {channel}")

        # Validate bitrate (optional for virtual and socketcan interfaces)
        if "bitrate" in config:
            bitrate = config["bitrate"]
            valid_bitrates = [125000, 250000, 500000, 1000000]
            if bitrate not in valid_bitrates:
                errors.append(f"Invalid bitrate: {bitrate}. Must be one of {valid_bitrates}")
        elif interface and interface not in ("virtual", "socketcan", ""):
            # Bitrate required for hardware interfaces (only check if interface is specified)
            errors.append(f"Bitrate is required for {interface} interface")

    # Validate CAN-FD data bitrate (only validate if fd_mode is enabled)
    if config.get("fd_mode", False) and "data_bitrate" in config:
        data_bitrate = config["data_bitrate"]
        if data_bitrate < 500000 or data_bitrate > 8000000:
            errors.append(
                f"Invalid CAN-FD data bitrate: {data_bitrate}. Must be between 500000 and 8000000"
            )
    # Note: data_bitrate has a default in schema,
    # so it should always be present if fd_mode is true

    # Validate timestamp_mode
    if "timestamp_mode" in config:
        valid_modes = ["auto", "absolute", "ignore"]
        if config["timestamp_mode"] not in valid_modes:
            errors.append(
                f"Invalid timestamp_mode: {config['timestamp_mode']}. Must be one of {valid_modes}"
            )

    # Validate config_json (should be valid JSON object)
    if "config_json" in config and config["config_json"]:
        try:
            parsed = json.loads(config["config_json"])
            if not isinstance(parsed, dict):
                errors.append('config_json must be a JSON object (e.g., {"key": "value"})')
        except json.JSONDecodeError as e:
            errors.append(f"Invalid JSON in config_json: {e}")

    return errors
