"""Unit tests for the cross-bus `can/tx/...` action router.

Covers raw + DBC send, duplicate-replace on periodics, multi-bus isolation,
DBC encode parity vs cantools, and the list_messages catalog shape.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import can
import cantools
import pytest

from zelos_extension_can.actions.router import (
    CanActionsRouter,
    _encode_dbc,
    _parse_can_id,
    _parse_data_hex,
    _parse_mux,
    _parse_signals_json,
    _task_id,
    _validate_id_range,
)
from zelos_extension_can.codec import CanCodec

DBC_PATH = Path(__file__).parent / "files" / "test.dbc"


@pytest.fixture
def test_dbc():
    return cantools.database.load_file(str(DBC_PATH))


@pytest.fixture
def codec_a():
    """Single-bus codec named 'busA' with a started mock bus."""
    with patch("zelos_sdk.TraceSource"), patch("can.Bus") as mock_bus_cls:
        cfg = {
            "interface": "virtual",
            "channel": "vcan0",
            "bitrate": 500_000,
            "database_file": str(DBC_PATH),
        }
        codec = CanCodec(cfg, bus_name="busA")
        codec.start()
        assert mock_bus_cls.called
        yield codec
        codec.stop()


@pytest.fixture
def codec_b():
    """Second-bus codec named 'busB' for multi-bus isolation tests."""
    with patch("zelos_sdk.TraceSource"), patch("can.Bus"):
        cfg = {
            "interface": "virtual",
            "channel": "vcan1",
            "bitrate": 500_000,
            "database_file": str(DBC_PATH),
        }
        codec = CanCodec(cfg, bus_name="busB")
        codec.start()
        yield codec
        codec.stop()


@pytest.fixture
def router(codec_a, codec_b):
    return CanActionsRouter({"busA": codec_a, "busB": codec_b})


# ─── Pure helpers (memory: feedback_test_at_helper_seam) ────────────────────


class TestPureHelpers:
    def test_parse_can_id_accepts_0x_and_bare_hex(self):
        assert _parse_can_id("0x100") == 0x100
        assert _parse_can_id("0X1FF") == 0x1FF
        assert _parse_can_id("100") == 0x100
        assert _parse_can_id("  0x7ff  ") == 0x7FF

    def test_parse_data_hex_tolerates_spaces_and_commas(self):
        assert _parse_data_hex("01 02 03 04") == b"\x01\x02\x03\x04"
        assert _parse_data_hex("01,02,03") == b"\x01\x02\x03"
        assert _parse_data_hex("") == b""

    def test_validate_id_range_standard_vs_extended(self):
        _validate_id_range(0x7FF, is_extended=False)
        with pytest.raises(ValueError, match="out of range for standard"):
            _validate_id_range(0x800, is_extended=False)
        _validate_id_range(0x1FFFFFFF, is_extended=True)
        with pytest.raises(ValueError, match="out of range for extended"):
            _validate_id_range(0x20000000, is_extended=True)

    def test_task_id_is_stable_across_payload_changes(self):
        # Stable key → starting a periodic twice with the same bus/ID/frame-kind
        # replaces the prior slot rather than spawning a sibling.
        a = _task_id("busA", 0x100, is_extended=False, mux="raw")
        b = _task_id("busA", 0x100, is_extended=False, mux="raw")
        assert a == b == "busA:0x100:std:raw"

    def test_task_id_distinguishes_std_vs_ext_and_buses(self):
        # Same arbitration ID on two buses must not collide into a single slot.
        assert _task_id("busA", 0x100, False) != _task_id("busB", 0x100, False)
        # Standard vs extended frames with the same numeric ID stay separate slots
        assert _task_id("busA", 0x100, False) != _task_id("busA", 0x100, True)

    def test_parse_mux_returns_none_int_or_label(self):
        assert _parse_mux("") is None
        assert _parse_mux("  ") is None
        assert _parse_mux("3") == 3
        assert _parse_mux("0x2") == 2
        assert _parse_mux("Reverse") == "Reverse"

    def test_parse_signals_json_rejects_non_object(self):
        assert _parse_signals_json('{"Speed": 50}') == {"Speed": 50}
        with pytest.raises(ValueError, match="JSON object"):
            _parse_signals_json("[1, 2]")
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_signals_json("{not json}")


# ─── Bus + action surface ────────────────────────────────────────────────────


class TestUnknownTargets:
    def test_unknown_bus_raises(self, router):
        with pytest.raises(ValueError, match="unknown bus"):
            router.list_messages(bus="ghost")

    def test_unknown_dbc_message_raises(self, router):
        with pytest.raises(ValueError, match="unknown DBC message"):
            router.send_message(bus="busA", message="NotAMessage", signals_json="{}")


class TestSendRaw:
    def test_calls_bus_send_with_correct_message(self, router, codec_a):
        result = router.send_raw(bus="busA", can_id="0x123", data="de ad be ef")
        # bus.send is called on the mocked codec.bus
        assert codec_a.bus.send.called
        sent_msg: can.Message = codec_a.bus.send.call_args.args[0]
        assert sent_msg.arbitration_id == 0x123
        assert sent_msg.data == b"\xde\xad\xbe\xef"
        assert sent_msg.is_extended_id is False
        # Result mirrors what was sent
        assert result["canId"] == 0x123
        assert result["dlc"] == 4
        assert result["dataHex"] == "deadbeef"

    def test_rejects_invalid_id_for_standard_frame(self, router):
        with pytest.raises(ValueError, match="out of range for standard"):
            router.send_raw(bus="busA", can_id="0x800", data="00")


class TestStartPeriodicRaw:
    def test_returns_task_id_and_not_replaced_first_time(self, router):
        async def go():
            r = router.start_periodic_raw(bus="busA", can_id="0x100", data="01", period_ms=50)
            assert r["task_id"] == "busA:0x100:std:raw"
            assert r["replaced"] is False
            router.stop_all()

        asyncio.run(go())

    def test_duplicate_replaces_and_returns_replaced_true(self, router):
        # Duplicate start_periodic_raw replaces the prior slot and signals
        # `replaced: True` to the caller so it can update its UI.
        async def go():
            first = router.start_periodic_raw(bus="busA", can_id="0x100", data="01", period_ms=50)
            second = router.start_periodic_raw(bus="busA", can_id="0x100", data="02", period_ms=50)
            assert first["task_id"] == second["task_id"]
            assert first["replaced"] is False
            assert second["replaced"] is True
            router.stop_all()

        asyncio.run(go())

    def test_multi_bus_same_id_distinct_slots(self, router, codec_a, codec_b):
        # Two buses with the same CAN ID get distinct task_ids; stopping one
        # leaves the other running.
        async def go():
            a = router.start_periodic_raw(bus="busA", can_id="0x100", data="aa", period_ms=50)
            b = router.start_periodic_raw(bus="busB", can_id="0x100", data="bb", period_ms=50)
            assert a["task_id"] != b["task_id"]
            assert a["replaced"] is False and b["replaced"] is False

            snap = router.get_tx_state()
            tids_per_bus = {
                bus["name"]: [p["taskId"] for p in bus["periodics"]] for bus in snap["buses"]
            }
            assert a["task_id"] in tids_per_bus["busA"]
            assert b["task_id"] in tids_per_bus["busB"]

            # Stopping one leaves the other untouched
            router.stop_periodic(task_id=a["task_id"])
            snap2 = router.get_tx_state()
            tids2 = {bus["name"]: [p["taskId"] for p in bus["periodics"]] for bus in snap2["buses"]}
            assert tids2["busA"] == []
            assert b["task_id"] in tids2["busB"]
            router.stop_all()

        asyncio.run(go())


class TestStopPeriodic:
    def test_unknown_task_id_returns_stopped_false(self, router):
        result = router.stop_periodic(task_id="busA:0xdeadbeef:std:raw")
        assert result == {"task_id": "busA:0xdeadbeef:std:raw", "stopped": False}

    def test_existing_task_returns_stopped_true(self, router):
        async def go():
            started = router.start_periodic_raw(bus="busA", can_id="0x200", data="ff", period_ms=50)
            stopped = router.stop_periodic(task_id=started["task_id"])
            assert stopped == {"task_id": started["task_id"], "stopped": True}
            # Slot is gone from the snapshot
            snap = router.get_tx_state()
            tids = [p["taskId"] for bus in snap["buses"] for p in bus["periodics"]]
            assert started["task_id"] not in tids

        asyncio.run(go())


class TestListMessages:
    def test_returns_dbc_catalog_for_bus(self, router):
        # list_messages reads from the bus's already-loaded DBC; the app never
        # parses its own copy.
        result = router.list_messages(bus="busA")
        assert result["bus"] == "busA"
        assert result["dbcName"] == "test.dbc"
        names = {m["name"] for m in result["messages"]}
        assert {"DUT_Status", "DUT_Command", "DUT_Logging"} <= names
        # Each message describes its signals (DUT_Status has known signal names)
        status = next(m for m in result["messages"] if m["name"] == "DUT_Status")
        signal_names = {s["name"] for s in status["signals"]}
        assert "state" in signal_names
        assert "SOC_signal" in signal_names


class TestSendMessage:
    def test_dbc_encode_matches_cantools(self, router, codec_a, test_dbc):
        # send_message bytes must match what cantools itself would encode for
        # the same message + signal dict.
        signals = {"state_request": 5}
        result = router.send_message(
            bus="busA", message="DUT_Command", signals_json=json.dumps(signals)
        )
        # Cantools reference encode for the same inputs
        expected = bytes(test_dbc.get_message_by_name("DUT_Command").encode(signals))
        sent_msg: can.Message = codec_a.bus.send.call_args.args[0]
        assert bytes(sent_msg.data) == expected
        assert result["dataHex"] == expected.hex()

    def test_multiplexed_message_routes_mux_into_signals(self, router, codec_a, test_dbc):
        # DUT_Logging multiplexes logging_signalN by logging_mux; the always-present
        # no_mux_logging_signal must also be supplied to satisfy cantools.
        signals = {"logging_signal0": 1, "no_mux_logging_signal": 0}
        router.send_message(
            bus="busA",
            message="DUT_Logging",
            signals_json=json.dumps(signals),
            mux="0",
        )
        sent_msg: can.Message = codec_a.bus.send.call_args.args[0]
        expected = bytes(
            test_dbc.get_message_by_name("DUT_Logging").encode(
                {"logging_mux": 0, "logging_signal0": 1, "no_mux_logging_signal": 0}
            )
        )
        assert bytes(sent_msg.data) == expected


class TestStartPeriodicMessage:
    def test_returns_task_id_and_replaced_semantics(self, router):
        async def go():
            payload = json.dumps({"state_request": 0})
            r1 = router.start_periodic_message(
                bus="busA", message="DUT_Command", signals_json=payload, period_ms=50
            )
            r2 = router.start_periodic_message(
                bus="busA", message="DUT_Command", signals_json=payload, period_ms=50
            )
            assert r1["task_id"] == r2["task_id"]
            assert r1["replaced"] is False
            assert r2["replaced"] is True
            router.stop_all()

        asyncio.run(go())


class TestGetTxState:
    def test_snapshot_shape_matches_app_contract(self, router):
        snap = router.get_tx_state()
        assert set(snap.keys()) >= {"capturedAtUnixMs", "extension", "buses"}
        assert snap["extension"]["id"] == "zeloscloud.zelos-extension-can"
        assert snap["extension"]["state"] == "running"
        bus_names = {b["name"] for b in snap["buses"]}
        assert bus_names == {"busA", "busB"}
        for bus in snap["buses"]:
            assert bus["status"] == "active"
            assert "metrics" in bus and "txErrors" in bus["metrics"]
            assert "messagesReceived" in bus["metrics"]
            assert bus["dbc"]["name"] == "test.dbc"
            assert isinstance(bus["periodics"], list)


class TestEncodeHelper:
    def test_encode_dbc_returns_bytes_matching_cantools(self, test_dbc):
        msg = test_dbc.get_message_by_name("DUT_Command")
        signals = {"state_request": 3}
        out = _encode_dbc(msg, signals, mux_value=None)
        assert isinstance(out, bytes)
        assert out == bytes(msg.encode(signals))

    def test_encode_dbc_injects_mux_signal_when_not_in_payload(self, test_dbc):
        msg = test_dbc.get_message_by_name("DUT_Logging")
        # logging_mux is the multiplexer; caller passes the mux'd signal, the always-present
        # no_mux_logging_signal, and a standalone mux=0 that the helper injects.
        out = _encode_dbc(
            msg,
            {"logging_signal0": 1, "no_mux_logging_signal": 0},
            mux_value=0,
        )
        assert out == bytes(
            msg.encode({"logging_mux": 0, "logging_signal0": 1, "no_mux_logging_signal": 0})
        )
