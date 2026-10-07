"""The gs_usb (USB) interface: config to GsUsbBus kwargs, the extra, the libusb shim."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from zelos_can.bus import prepare_bus_config
from zelos_can.bus.factory import open_python_can_bus

from zelos_extension_can import INTERFACES, gs_usb
from zelos_extension_can.cli.app import _create_codecs, resolve_advanced

TEST_DBC = Path(__file__).parent / "files" / "test.dbc"
SCHEMA = json.loads((Path(__file__).parents[1] / "config.schema.json").read_text())


def _bus_kwargs(bus: dict) -> dict:
    """The kwargs a form bus entry opens python-can's Bus() with."""
    config = gs_usb.bus_config({**bus, "interface": INTERFACES[bus["interface"]]})
    with patch("can.Bus") as bus_cls:
        open_python_can_bus(prepare_bus_config(config, TEST_DBC), "bus")
    return bus_cls.call_args.kwargs


def test_label_maps_to_python_can_gs_usb():
    assert INTERFACES["gs_usb (USB)"] == "gs_usb"


def test_kwargs_from_index_and_from_bus_address():
    by_index = _bus_kwargs(
        {"interface": "gs_usb (USB)", "channel": 1, "bitrate": 250000, "config_json": ""}
    )
    assert by_index == {
        "interface": "gs_usb",
        "channel": 1,
        "bitrate": 250000,
        "receive_own_messages": True,
    }
    # Index defaults to the first adapter.
    assert _bus_kwargs({"interface": "gs_usb (USB)", "bitrate": 500000})["channel"] == 0

    by_address = _bus_kwargs(
        {
            "interface": "gs_usb (USB)",
            "usb_bus": 3,
            "usb_address": 7,
            "bitrate": 500000,
            "config_json": '{"receive_own_messages": false}',
        }
    )
    assert by_address == {
        "interface": "gs_usb",
        "channel": 0,
        "bitrate": 500000,
        "bus": 3,
        "address": 7,
        "receive_own_messages": False,
    }


def test_index_with_bus_address_is_rejected():
    both = {"interface": "gs_usb (USB)", "channel": 0, "usb_bus": 1, "usb_address": 2}
    with pytest.raises(gs_usb.GsUsbConfigError, match="not both"):
        gs_usb.bus_config(both)
    with pytest.raises(gs_usb.GsUsbConfigError, match="together"):
        gs_usb.bus_config({"interface": "gs_usb (USB)", "usb_bus": 1})

    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft7Validator(SCHEMA)
    assert not validator.is_valid({"buses": [{**both, "bitrate": 500000}]})
    del both["channel"]
    assert validator.is_valid({"buses": [{**both, "bitrate": 500000}]})


def test_missing_extra_is_one_line_naming_the_extra():
    with (
        patch.dict(sys.modules, {"gs_usb": None}),
        pytest.raises(gs_usb.GsUsbConfigError) as e,
    ):
        gs_usb.require_extra()
    assert "gs_usb extra" in str(e.value)
    assert "\n" not in str(e.value)


def test_libusb_shim_runs_only_for_gs_usb():
    gs = {"interface": "gs_usb (USB)", "bitrate": 500000}
    vbus = {
        "interface": "Other (python-can)",
        "config_json": json.dumps({"interface": "virtual", "channel": "vcan0"}),
    }
    advanced = resolve_advanced({})
    with (
        patch("zelos_sdk.TraceSource"),
        patch.object(gs_usb, "require_extra"),
        patch.object(gs_usb, "prime_libusb") as prime,
    ):
        _create_codecs({"buses": [vbus]}, TEST_DBC, advanced)
        prime.assert_not_called()
        pairs = _create_codecs({"buses": [gs]}, TEST_DBC, advanced)
        prime.assert_called_once()
    assert [name for _, name in pairs] == ["gs_usb0"]


def test_prime_libusb_loads_libusb_package_library():
    libusb1 = MagicMock()
    finder = SimpleNamespace(find_library=object())
    usb = SimpleNamespace(backend=SimpleNamespace(libusb1=libusb1))
    modules = {"libusb_package": finder, "usb": usb, "usb.backend": usb.backend}
    modules["usb.backend.libusb1"] = libusb1
    with patch.dict(sys.modules, modules):
        gs_usb.prime_libusb()
    libusb1.get_backend.assert_called_once_with(find_library=finder.find_library)
