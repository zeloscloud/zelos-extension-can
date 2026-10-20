"""ARXML through the extension's bus config: `cluster` picks the bus's
cluster, and E2E status reaches the trace on both lanes (zelos-virtual and a
python-can bus). Decode itself is zelos-can's to test.

two-clusters.arxml: CanA carries Status (0x010, E2E Profile 2: CRC byte 0,
counter byte 1, Value byte 2), CanB carries Other (0x020, Speed byte 0).
"""

import json
import time
from pathlib import Path

import can
import pytest
import zelos_sdk
from zelos_can import VirtualBus

from zelos_extension_can.cli.app import _create_codecs, resolve_advanced

ARXML = Path(__file__).parent / "files" / "two-clusters.arxml"
STATUS_OK = bytes.fromhex("8901070000000000")  # Value 7, counter 1, valid CRC
STATUS_BAD_CRC = bytes.fromhex("7601070000000000")


def _bus(name, interface, channel, **extra):
    return {
        "interface": "Other (python-can)",
        "name": name,
        "config_json": json.dumps({"interface": interface, "channel": channel}),
        "database_files": [str(ARXML)],
        **extra,
    }


def _wait(cache, path, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (value := cache[path].get()) is not None and want(value):  # None until logged
                return value
        except (KeyError, AttributeError):
            pass
        time.sleep(0.02)
    pytest.fail(f"no matching value at {path}")


def test_cluster_selects_each_bus_and_e2e_status_reaches_the_trace():
    cache = zelos_sdk.TraceSourceCache("CAN")
    config = {
        "buses": [
            _bus("pycan", "virtual", "arxml_pycan", cluster="CanA"),
            _bus("zv", "zelos-virtual", "arxml_zv", cluster="CanB"),
        ]
    }
    pairs = _create_codecs(config, ARXML, resolve_advanced({}), cache)
    peer = can.Bus(interface="virtual", channel="arxml_pycan")
    try:
        for codec, _ in pairs:
            codec.start()
        assert [c.list_messages()["messages"][0]["key"] for c, _ in pairs] == [
            "0010_Status",
            "0020_Other",
        ]
        # python-can lane: E2E is decoded only on zelos-can's Rust decoder
        peer.send(can.Message(arbitration_id=0x10, data=STATUS_OK, is_extended_id=False))
        assert _wait(cache, "pycan/0010_Status/Value", lambda v: True) == 7
        ok = _wait(cache, "pycan/0010_Status/e2e_status", lambda v: True)
        assert ok == 0  # ok
        peer.send(can.Message(arbitration_id=0x10, data=STATUS_BAD_CRC, is_extended_id=False))
        assert _wait(cache, "pycan/0010_Status/e2e_status", lambda v: v != ok) != ok
        VirtualBus("arxml_zv").send(id=0x20, data=bytes([55, 0, 0, 0, 0, 0, 0, 0]))
        assert _wait(cache, "zv/0020_Other/Speed", lambda v: True) == 55
    finally:
        peer.shutdown()
        for codec, _ in pairs:
            codec.stop()


def test_a_multi_cluster_arxml_without_cluster_refuses_and_lists_them():
    config = {"buses": [_bus("zv", "zelos-virtual", "arxml_none")]}
    with pytest.raises(Exception, match="CanA, CanB"):
        _create_codecs(config, ARXML, resolve_advanced({}), zelos_sdk.TraceSourceCache("CAN"))
