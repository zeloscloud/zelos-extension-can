"""Tests for configuration loading."""

import json
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

from zelos_extension_can.utils.config import load_config


def test_load_config_from_file(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """Test config loads from file first.

    :param tmp_path: Pytest temporary directory fixture
    :param monkeypatch: Pytest monkeypatch fixture
    """
    monkeypatch.chdir(tmp_path)

    # Create both schema and config files
    schema: dict[str, Any] = {"properties": {"interval": {"type": "number", "default": 1.0}}}
    with open(tmp_path / "config.schema.json", "w") as f:
        json.dump(schema, f)

    config_data: dict[str, Any] = {"interval": 2.5}
    with open(tmp_path / "config.json", "w") as f:
        json.dump(config_data, f)

    # Should prefer config.json over schema defaults
    config = load_config()
    assert config["interval"] == 2.5


def test_load_config_from_schema_defaults(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """Test config falls back to schema defaults.

    :param tmp_path: Pytest temporary directory fixture
    :param monkeypatch: Pytest monkeypatch fixture
    """
    monkeypatch.chdir(tmp_path)

    # Create only schema file
    schema: dict[str, Any] = {
        "properties": {
            "sensor_name": {"type": "string", "default": "sensor-01"},
            "interval": {"type": "number", "default": 0.1},
        }
    }
    with open(tmp_path / "config.schema.json", "w") as f:
        json.dump(schema, f)

    config = load_config()
    assert config["sensor_name"] == "sensor-01"
    assert config["interval"] == 0.1


def test_load_config_empty_fallback(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """Test config returns empty dict when nothing exists.

    :param tmp_path: Pytest temporary directory fixture
    :param monkeypatch: Pytest monkeypatch fixture
    """
    monkeypatch.chdir(tmp_path)

    config = load_config()
    assert config == {}
