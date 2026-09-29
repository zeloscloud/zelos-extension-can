"""The library surface other extensions import."""

import subprocess
import sys
from pathlib import Path

import pytest

from zelos_extension_can.bus import BusConfigError, prepare_bus_config


def test_bus_imports_without_decode_demo_or_actions():
    """A fresh interpreter: `bus` alone must not pull in cantools, the demo
    simulator, the codec or the actions module; the package root still
    exports `CanCodec` on access."""
    probe = (
        "import sys, zelos_extension_can.bus\n"
        "heavy = ('cantools', 'zelos_extension_can.demo.demo', 'zelos_extension_can.codec',"
        " 'zelos_extension_can.actions')\n"
        "print(sorted(m for m in heavy if m in sys.modules))\n"
        "from zelos_extension_can import ACTION_PREFIX, CanCodec\n"
        "print(ACTION_PREFIX, CanCodec.__module__)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout
    assert out.splitlines() == ["[]", "CAN zelos_extension_can.codec"]


@pytest.mark.parametrize(
    "bus",
    [
        {"interface": "other"},
        {"interface": "other", "config_json": "{"},
        {"interface": "other", "config_json": '{"interface": "pcan"}'},
        {"interface": "ssh-socketcan"},
    ],
)
def test_prepare_bus_config_raises_instead_of_exiting(bus):
    with pytest.raises(BusConfigError):
        prepare_bus_config(bus, Path("demo.dbc"))
