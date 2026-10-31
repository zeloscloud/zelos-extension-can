"""Layouts saved by Zelos 26.0.9 and earlier find the Bus Monitor by these ids."""

import tomllib
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def test_saved_layouts_find_the_bus_monitor() -> None:
    manifest = tomllib.loads((ROOT / "extension.toml").read_text())
    owner_repo = urlparse(manifest["repository"]).path.strip("/")
    assert owner_repo.lower().replace("/", ".", 1) == "zeloscloud.zelos-extension-can"
    assert "bus-monitor" in {panel["id"] for panel in manifest["app"]["panels"]}
