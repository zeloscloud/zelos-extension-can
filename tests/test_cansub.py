"""The CANsub interface: config to CanSub kwargs, mDNS discovery, auto-configure keys."""

import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
from zelos_can.bus import prepare_bus_config
from zelos_can.bus.factory import open_python_can_bus

from zelos_extension_can import INTERFACES, actions, cansub

TEST_DBC = Path(__file__).parent / "files" / "test.dbc"


def _bus_kwargs(bus: dict) -> tuple[str, dict]:
    """The default name and the kwargs a form bus entry opens python-can's Bus() with."""
    config = cansub.bus_config({**bus, "interface": INTERFACES[bus["interface"]]})
    with patch("can.Bus") as bus_cls:
        open_python_can_bus(prepare_bus_config(config, TEST_DBC), "bus")
    return config["name"], bus_cls.call_args.kwargs


def test_kwargs_and_default_name():
    usb = {"interface": "CANsub", "address": "AABBCCDD-usb.local", "channel": 2, "bitrate": 500000}
    name, kwargs = _bus_kwargs(usb)
    # Over Ethernet the same device keeps the same name.
    assert name == "cansub_aabbccdd_ch2" == _bus_kwargs({**usb, "address": "aabbccdd-eth.local"})[0]
    assert kwargs == {
        "interface": "cansub",
        "channel": "AABBCCDD-usb.local@2",
        "bitrate": 500000,
        "receive_own_messages": True,
        "shutdown_timeout": 0.5,
    }

    ip = {
        "interface": "CANsub",
        "address": "192.168.1.10",
        "channel": 1,
        "bitrate": 500000,
        "fd_mode": True,
        "data_bitrate": 2000000,
        "listen_only": True,
        "ptp_synced": True,
        "config_json": '{"shutdown_timeout": 2.0}',
    }
    name, kwargs = _bus_kwargs(ip)
    assert name == "cansub_192_168_1_10_ch1"
    # The user's config_json wins.
    assert kwargs == {
        "interface": "cansub",
        "channel": "192.168.1.10@1",
        "bitrate": 500000,
        "fd": True,
        "data_bitrate": 2000000,
        "receive_own_messages": True,
        "shutdown_timeout": 2.0,
        "listen_only": True,
        "max_device_time_skew": None,
    }
    assert _bus_kwargs({**ip, "name": "chassis"})[0] == "chassis"


def test_discover_maps_channels_and_notes_api_mismatch(monkeypatch):
    lib = pytest.importorskip("python_can_cansub")

    def mdns_discover(interfaces, discovery_time):
        assert not any(ip.startswith("127.") for ip in interfaces)
        warnings.warn("Device x-usb.local has an unsupported API version (04.00)", stacklevel=1)
        return [
            {"interface": "cansub", "channel": "aabbccdd-usb.local@1"},
            {"interface": "cansub", "channel": "aabbccdd-usb.local@2"},
            # The same device over Ethernet is listed once.
            {"interface": "cansub", "channel": "aabbccdd-eth.local@1"},
        ]

    monkeypatch.setattr(lib.CanSub, "mdns_discover", mdns_discover)
    found, note = cansub.discover()
    assert found == [
        {
            "interface": "cansub",
            "address": "aabbccdd-usb.local",
            "channel": n,
            "name": f"CANsub aabbccdd ch{n}",
        }
        for n in (1, 2)
    ]
    assert "API version (04.00)" in note
    assert note.endswith("Update the extension or the device firmware.")


def test_auto_config_matches_form_buses_by_address_and_channel(monkeypatch):
    monkeypatch.setattr(actions, "_local_can_interfaces", list)
    monkeypatch.setattr(actions, "_vendor_adapters", list)
    monkeypatch.setattr(actions, "_slcan_adapters", list)
    monkeypatch.setattr(actions.gs_usb, "discover", lambda: ([], {}, ""))
    monkeypatch.setattr(cansub, "extra_installed", lambda: True)
    found = [
        {
            "interface": "cansub",
            "address": f"{d}-usb.local",
            "channel": 1,
            "name": f"CANsub {d} ch1",
        }
        for d in ("aa", "bb")
    ]
    monkeypatch.setattr(cansub, "discover", lambda: (found, None))
    mine = {"interface": "CANsub", "address": "BB-usb.local", "channel": 1, "bitrate": 250000}
    result = actions.auto_config({"buses": [mine]})
    assert result["config"]["buses"] == [
        {
            "interface": "CANsub",
            "address": "aa-usb.local",
            "channel": 1,
            "bitrate": 500000,
            "database_files": [],
        },
        mine,
    ]
    assert result["message"].startswith("Found CANsub aa ch1 on CANsub aa-usb.local@1, ")
