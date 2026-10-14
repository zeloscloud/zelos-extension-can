"""extension.toml [extras] mirrors pyproject: uv installs from it, the app lists the manifest."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_extras_match_pyproject() -> None:
    extras = tomllib.loads((ROOT / "extension.toml").read_text())["extras"]
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    optional = pyproject["project"]["optional-dependencies"]
    assert extras == {name: optional[name] for name in extras}
