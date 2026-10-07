"""extension.toml [extras] mirrors pyproject: uv installs from it, the app lists the manifest."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_extras_match_pyproject() -> None:
    extras = tomllib.loads((ROOT / "extension.toml").read_text())["extras"]
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    optional = pyproject["project"]["optional-dependencies"]
    assert {name: extra["packages"] for name, extra in extras.items()} == {
        name: optional[name] for name in extras
    }


def test_each_extra_says_what_it_enables() -> None:
    # The app shows the description beside the extra's checkbox, on one line.
    extras = tomllib.loads((ROOT / "extension.toml").read_text())["extras"]
    for name, extra in extras.items():
        description = extra.get("description", "")
        assert description.strip(), name
        assert "\n" not in description and len(description) <= 60, name
