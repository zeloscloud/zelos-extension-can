"""Configuration loading."""

import json
from pathlib import Path
from typing import Any


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
