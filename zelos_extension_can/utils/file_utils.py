"""File utilities for handling data URLs and file conversions."""

import base64
import os
from pathlib import Path


def data_url_to_file(data_url: str, output_path: str, detect_extension: bool = False) -> str:
    """Convert data-url (base64 encoded file) to a file on disk.

    :param data_url: Data URL in format "data:mime/type;base64,<encoded_data>"
    :param output_path: Path where to save the decoded file
    :param detect_extension: If True, try to detect file extension from content
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

    # Use absolute path or write to extension data directory if relative
    output_path_obj = Path(output_path)
    if not output_path_obj.is_absolute():
        # Get extension root directory from ZELOS_CONFIG_PATH
        # Config is at: /path/to/extension/root/config.json
        # We want to write to: /path/to/extension/root/data/<filename>
        config_path = os.environ.get("ZELOS_CONFIG_PATH")
        if not config_path:
            raise RuntimeError("ZELOS_CONFIG_PATH environment variable not set")

        # Use extension root/data directory
        ext_root = Path(config_path).parent
        data_dir = ext_root / "data"
        data_dir.mkdir(exist_ok=True)
        output_path_obj = data_dir / output_path_obj.name

    if detect_extension:
        # Detect CAN database file format from content
        content_start = file_bytes[:100].decode("latin-1", errors="ignore")
        if content_start.startswith("<?xml") or "<AUTOSAR" in content_start:
            # ARXML file
            output_path_obj = output_path_obj.with_suffix(".arxml")
        elif content_start.startswith("VERSION"):
            # DBC file
            output_path_obj = output_path_obj.with_suffix(".dbc")
        elif "<NetworkDefinition" in content_start or "<KCD" in content_start:
            # KCD file
            output_path_obj = output_path_obj.with_suffix(".kcd")
        elif "FormatVersion" in content_start and "TitleBlock" in content_start:
            # SYM file
            output_path_obj = output_path_obj.with_suffix(".sym")
        else:
            # Default to .dbc if unknown
            output_path_obj = output_path_obj.with_suffix(".dbc")

    output_path_obj.parent.mkdir(parents=True, exist_ok=True)

    with Path.open(output_path_obj, "wb") as f:
        f.write(file_bytes)

    return str(output_path_obj)
