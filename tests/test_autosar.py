"""AUTOSAR ARXML end to end through the extension: two buses on zelos-can's
in-memory bus, cantools-encoded frames in, decoded PDUs and E2E status out.

system-4.2.arxml (cantools' fixture) has one CAN cluster; the generated
someip-system-00048.arxml has two, so its bus names `cluster`.
"""

import json
import time
from pathlib import Path

import cantools
import pytest
import zelos_sdk
from cantools.autosar.end_to_end import apply_profile2_crc, apply_profile5_crc
from zelos_can import VirtualBus

from zelos_extension_can.cli.app import _create_codecs, resolve_advanced

FILES = Path(__file__).parent / "files"
SYSTEM = FILES / "system-4.2.arxml"
SOMEIP = FILES / "someip-system-00048.arxml"
E2E_OK = 0


def _bus(name, channel, database, **extra):
    return {
        "interface": "Other (python-can)",
        "name": name,
        "config_json": json.dumps({"interface": "zelos-virtual", "channel": channel}),
        "database_files": [str(database)],
        **extra,
    }


def _wait(cache, path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return cache[path].get()
        except (KeyError, AttributeError):
            time.sleep(0.02)
    pytest.fail(f"no value at {path}")


@pytest.fixture
def buses():
    cache = zelos_sdk.TraceSourceCache("CAN")
    config = {
        "buses": [
            _bus("sys42", "autosar_sys42", SYSTEM),
            _bus("can1", "autosar_can1", SOMEIP, cluster="Can1"),
        ]
    }
    pairs = _create_codecs(config, SYSTEM, resolve_advanced({}), cache)
    for codec, _ in pairs:
        codec.start()
    yield cache, {name: VirtualBus(codec.config["channel"]) for codec, name in pairs}
    for codec, _ in pairs:
        codec.stop()


def test_arxml_pdus_decode_by_pdu_name_with_e2e_status(buses):
    cache, vbus = buses
    db = cantools.database.load_file(str(SYSTEM))

    def send(bus, msg, data):
        vbus[bus].send(
            id=msg.frame_id,
            data=bytes(data),
            is_extended=msg.is_extended_frame,
            is_fd=len(data) > 8,
        )

    # E2E profile 2, counter 1
    m1 = db.get_message_by_name("Message1")
    data = bytearray(m1.encode({s.name: 0 for s in m1.signals} | {"signal1": 15}, strict=False))
    data[1] = (data[1] & 0xF0) | 1
    send("sys42", m1, apply_profile2_crc(bytes(data), m1))
    # secured, E2E profile 5 on the authentic PDU, counter 1
    m3 = db.get_message_by_name("Message3")
    data = bytearray(m3.encode({s.name: 0 for s in m3.signals}, strict=False))
    data[2] = 1
    send("sys42", m3, apply_profile5_crc(bytes(data[:4]), m3) + bytes(data[4:]))
    # container: message2 and the multiplexed PDU at selector 1
    box = db.get_message_by_name("OneToContainThemAll")
    m2 = box.get_contained_message_by_name("message2")
    mux = box.get_contained_message_by_name("multiplexed_message")
    mux_values = {s.name: 0 for s in mux.signals if s.multiplexer_ids in (None, [1])}
    mux_values |= {"OneToContainThemAll_selector1": 1, "World1": 1}
    send(
        "sys42",
        box,
        box.encode([(m2, {"signal3": 2, "signal2": -100, "signal4": 9}), (mux, mux_values)]),
    )

    assert _wait(cache, "sys42/0005_message1/signal1") == 15
    assert _wait(cache, "sys42/0005_message1/e2e_status") == E2E_OK
    assert _wait(cache, "sys42/0064_message3_secured/e2e_status") == E2E_OK
    assert _wait(cache, "sys42/0064_message3_secured/secoc") == 0  # unverified
    assert _wait(cache, "sys42/0066_message2/signal2") == -100
    assert _wait(cache, "sys42/0066_multiplexed_message/multiplexed_message_1/World1") == 1

    # the cluster-selected bus: Can1's container and secured PDU
    can1 = cantools.database.load_file(str(SOMEIP))
    cf0 = next(m for m in can1.messages if m.name == "CF0")
    c0 = {c.name: c for c in cf0.contained_messages}
    send(
        "can1",
        cf0,
        cf0.encode(
            [(c0["C0_1"], {"C0_1_0": 7, "C0_1_1": 8}), (c0["C0_0"], {"C0_0_0": 5, "C0_0_1": 6})]
        ),
    )
    sf0 = next(m for m in can1.messages if m.name == "SF0")
    send("can1", sf0, bytes(range(16)))
    assert _wait(cache, "can1/1f000000_C0_1/C0_1_0") == 7
    assert _wait(cache, "can1/1f000000_C0_0/C0_0_1") == 6
    assert _wait(cache, "can1/1e000000_SEC0/secoc") == 0
