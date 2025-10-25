"""File and platfutils validation."""

import base64
from pathlib import Path


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
