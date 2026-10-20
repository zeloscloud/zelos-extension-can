"""AUTOSAR ARXML end to end through the extension: two buses on zelos-can's
in-memory bus, cantools-encoded frames in, decoded PDUs under their frames and
E2E status out;
and a python-can bus decoding a real ECU's capture.

system-4.2.arxml (cantools' fixture) has one CAN cluster; the generated
someip-system-00048.arxml has two, so its bus names `cluster`. can-full.arxml
describes the zelos-examples CAN bench, and can-full-10s.candump is 10 s of it.
"""

import json
import time
from collections import Counter
from pathlib import Path

import can
import cantools
import pyarrow as pa
import pytest
import zelos_sdk
from cantools.autosar.end_to_end import apply_profile2_crc, apply_profile5_crc
from zelos_can import VirtualBus

from zelos_extension_can.cli.app import _create_codecs, resolve_advanced

FILES = Path(__file__).parent / "files"
SYSTEM = FILES / "system-4.2.arxml"
SOMEIP = FILES / "someip-system-00048.arxml"
CAN_FULL = FILES / "can-full.arxml"
TEST_DBC = FILES / "test.dbc"
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
            if (value := cache[path].get()) is not None:  # None until first logged
                return value
        except (KeyError, AttributeError):
            pass
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


def test_arxml_pdus_decode_under_their_frame_with_e2e_status(buses):
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

    box_event = "sys42/0066_OneToContainThemAll"
    assert _wait(cache, "sys42/0005_Message1/signal1") == 15
    assert _wait(cache, "sys42/0005_Message1/e2e_status") == E2E_OK
    assert _wait(cache, "sys42/0064_Message3/e2e_status") == E2E_OK
    assert _wait(cache, "sys42/0064_Message3/secoc") == 0  # unverified
    assert _wait(cache, f"{box_event}/message2/signal2") == -100
    assert _wait(cache, f"{box_event}/multiplexed_message/multiplexed_message_1/World1") == 1

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
    assert _wait(cache, "can1/1f000000_CF0/C0_1/C0_1_0") == 7
    assert _wait(cache, "can1/1f000000_CF0/C0_0/C0_0_1") == 6
    assert _wait(cache, "can1/1e000000_SF0/secoc") == 0


def test_a_python_can_bus_decodes_arxml_on_the_rust_decoder(tmp_path):
    """python-can's `virtual` bus with an ARXML database: the frames reach
    zelos-can's Rust decoder, so 10 s of the can-full ECU decode as on
    zelos-virtual (container, multiplexed, E2E, SecOC), at the frames' own
    timestamps, and its messages are addressed by frame name."""
    frames = []
    for line in (FILES / "can-full-10s.candump").read_text().splitlines():
        stamp, _, frame = line.split()
        can_id, data = frame.split("#")
        frames.append((float(stamp.strip("()")), int(can_id, 16), bytes.fromhex(data)))
    config = {
        "buses": [
            {
                "interface": "Other (python-can)",
                "name": "pycan",
                "config_json": json.dumps({"interface": "virtual", "channel": "autosar_pycan"}),
                "database_files": [str(CAN_FULL)],
            }
        ],
        "advanced": {"timestamp_mode": "absolute"},
    }
    namespace = zelos_sdk.TraceNamespace("autosar_pycan")
    trz = tmp_path / "pycan.trz"
    with zelos_sdk.TraceWriter(str(trz), namespace=namespace):
        source = zelos_sdk.TraceSource("CAN", namespace=namespace)
        ((codec, _),) = _create_codecs(config, SYSTEM, resolve_advanced(config), source)
        codec.start()
        peer = can.Bus(interface="virtual", channel="autosar_pycan", preserve_timestamps=True)
        try:
            for stamp, can_id, data in frames:
                peer.send(
                    can.Message(
                        timestamp=stamp, arbitration_id=can_id, data=data, is_extended_id=False
                    )
                )
            deadline = time.monotonic() + 10
            while codec.get_tx_state()["bus"]["metrics"]["messages_received"] < len(frames):
                assert time.monotonic() < deadline, codec.get_tx_state()["bus"]["metrics"]
                time.sleep(0.01)
            assert codec.describe_message("0310_DcdcOutputP1")["message"]["can_id"] == 0x310
        finally:
            peer.shutdown()
            codec.stop()

    with zelos_sdk.TraceReader(str(trz)) as reader:
        segments = [s.id for s in reader.list_data_segments()]
        span = reader.time_range()

        def column(field, name=None):
            result = reader.query(
                data_segment_ids=segments,
                fields=[f"*/CAN/pycan/{field}"],
                start=span.start,
                end=span.end,
            )
            table = pa.ipc.open_stream(result.to_arrow()).read_all()
            return table.column(name or f"CAN/pycan/{field}").to_pylist()

        e2e = {"0310_DcdcOutputP1": 503, "0311_DcdcThermalP2": 201, "0312_DcdcPowerP5": 503}
        for event, rows in e2e.items():
            assert Counter(column(f"{event}.e2e_status")) == {E2E_OK: rows}, event
        assert Counter(column("0315_DcdcSecured.secoc")) == {0: 100}  # unverified
        # the container's three PDUs, the multiplexed PDU's two dynamic parts
        counts = {
            "0313_DcdcContainer/DcdcHealth.UptimeS": 167,
            "0313_DcdcContainer/DcdcInput.InputVoltage": 168,
            "0313_DcdcContainer/DcdcRail.OutputVoltage": 168,
            "0314_DcdcMux/DcdcMuxCurrents.OutputCurrent": 100,
            "0314_DcdcMux/DcdcMuxLimits.AuxCurrentLimit": 101,
        }
        assert {field: len(column(field)) for field in counts} == counts
        stamps = column("Frame.arbitration_id", "time_s")
        assert (stamps[0], stamps[-1]) == pytest.approx((frames[0][0], frames[-1][0]), abs=1e-6)


@pytest.mark.parametrize("receive_own", [False, True])
def test_zelos_virtual_decodes_its_own_periodic_only_with_receive_own_messages(receive_own):
    """A periodic this bus sends decodes only with Receive Own Messages; a
    peer's frame always does."""
    channel = f"autosar_own_{receive_own}"
    config = {
        "buses": [_bus("own", channel, TEST_DBC)],
        "advanced": {"receive_own_messages": receive_own},
    }
    ((codec, _),) = _create_codecs(
        config, TEST_DBC, resolve_advanced(config), zelos_sdk.TraceSourceCache("CAN")
    )
    peer = VirtualBus(channel)
    codec.start()
    try:
        codec.start_periodic_raw("0x64", "0102030405060708", period_ms=10)
        sent = 0
        while sent < 5:
            frame = peer.recv(timeout=2.0)
            assert frame is not None
            sent += frame.arbitration_id == 0x64
        # A peer's frame of no message: decoded once everything before it is.
        peer.send(id=0x7FF, data=bytes(1))
        deadline = time.monotonic() + 5
        while (metrics := codec.get_tx_state()["bus"]["metrics"])["unknown_messages"] < 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        codec.stop()
    if receive_own:
        assert metrics["messages_received"] > sent
    else:
        assert metrics["messages_received"] == 1
