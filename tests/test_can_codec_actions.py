"""Unit tests for the CAN codec's action surface.

Each registered codec exposes its actions under ``can/<bus_name>/<method>`` on
the agent. These tests exercise the methods directly on a ``CanCodec`` instance
with a mocked python-can bus, covering raw + DBC send, duplicate-replace on
periodics, multi-bus isolation (two codecs share nothing), DBC encode parity
vs cantools, and the list_messages catalog shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import can
import cantools
import pytest

from zelos_extension_can.codec import (
    CanCodec,
    _derive_bus_status,
    _encode_dbc,
    _parse_can_id,
    _parse_data_hex,
    _parse_mux,
    _parse_signals_json,
    _task_id,
    _validate_id_range,
)

DBC_PATH = Path(__file__).parent / "files" / "test.dbc"


@pytest.fixture
def test_dbc():
    return cantools.database.load_file(str(DBC_PATH))


def _make_codec(bus_name: str = "busA", channel: str = "vcan0") -> CanCodec:
    """Build a CanCodec with a mocked python-can bus that records `send` calls."""
    with patch("zelos_sdk.TraceSource"), patch("can.Bus"):
        cfg = {
            "interface": "virtual",
            "channel": channel,
            "bitrate": 500_000,
            "database_file": str(DBC_PATH),
        }
        codec = CanCodec(cfg, bus_name=bus_name)
        codec.start()
        return codec


@pytest.fixture
def codec():
    c = _make_codec("busA", "vcan0")
    yield c
    c.stop()


@pytest.fixture
def codec_b():
    c = _make_codec("busB", "vcan1")
    yield c
    c.stop()


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
        # Same key for the same CAN ID + frame kind on the same bus →
        # starting the periodic twice replaces the prior slot.
        a = _task_id(0x100, is_extended=False, mux="raw")
        b = _task_id(0x100, is_extended=False, mux="raw")
        assert a == b == "0x100:std:raw"

    def test_task_id_distinguishes_std_vs_ext(self):
        # Standard vs extended frames with the same numeric ID stay separate slots.
        assert _task_id(0x100, False) != _task_id(0x100, True)

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


# ─── Action surface ─────────────────────────────────────────────────────────


class TestSendRaw:
    def test_calls_bus_send_with_correct_message(self, codec):
        result = codec.send_raw(can_id="0x123", data="de ad be ef")
        assert codec.bus.send.called
        sent_msg: can.Message = codec.bus.send.call_args.args[0]
        assert sent_msg.arbitration_id == 0x123
        assert sent_msg.data == b"\xde\xad\xbe\xef"
        assert sent_msg.is_extended_id is False
        assert result["can_id"] == 0x123
        assert result["dlc"] == 4
        assert result["data_hex"] == "deadbeef"

    def test_rejects_invalid_id_for_standard_frame(self, codec):
        with pytest.raises(ValueError, match="out of range for standard"):
            codec.send_raw(can_id="0x800", data="00")

    def test_raises_when_bus_is_stopped(self, codec):
        codec.stop()
        with pytest.raises(RuntimeError, match="not running"):
            codec.send_raw(can_id="0x100", data="00")


class TestStartPeriodicRaw:
    def test_returns_task_id_and_not_replaced_first_time(self, codec):
        r = codec.start_periodic_raw(can_id="0x100", data="01", period_ms=50)
        assert r["task_id"] == "0x100:std:raw"
        assert r["replaced"] is False

    def test_duplicate_replaces_and_returns_replaced_true(self, codec):
        # Duplicate start_periodic_raw replaces the prior slot and signals
        # `replaced: True` to the caller so it can update its UI.
        first = codec.start_periodic_raw(can_id="0x100", data="01", period_ms=50)
        second = codec.start_periodic_raw(can_id="0x100", data="02", period_ms=50)
        assert first["task_id"] == second["task_id"]
        assert first["replaced"] is False
        assert second["replaced"] is True

    def test_two_codecs_share_no_state(self, codec, codec_b):
        # Each codec is its own bus with its own periodic registry. They can
        # independently hold a slot with the same task_id (the bus is implicit
        # in the codec instance); stopping one does not affect the other.
        codec.start_periodic_raw(can_id="0x100", data="aa", period_ms=50)
        assert len(codec.get_tx_state()["bus"]["periodics"]) == 1
        assert codec_b.get_tx_state()["bus"]["periodics"] == []

        codec_b.start_periodic_raw(can_id="0x100", data="bb", period_ms=50)
        assert len(codec.get_tx_state()["bus"]["periodics"]) == 1
        assert len(codec_b.get_tx_state()["bus"]["periodics"]) == 1

        codec.stop_periodic(task_id="0x100:std:raw")
        assert codec.get_tx_state()["bus"]["periodics"] == []
        assert len(codec_b.get_tx_state()["bus"]["periodics"]) == 1


class TestStopPeriodic:
    def test_unknown_task_id_returns_stopped_false(self, codec):
        assert codec.stop_periodic(task_id="0xdeadbeef:std:raw") == {
            "task_id": "0xdeadbeef:std:raw",
            "stopped": False,
        }

    def test_existing_task_returns_stopped_true_and_clears_slot(self, codec):
        started = codec.start_periodic_raw(can_id="0x200", data="ff", period_ms=50)
        stopped = codec.stop_periodic(task_id=started["task_id"])
        assert stopped == {"task_id": started["task_id"], "stopped": True}
        tids = {p["task_id"] for p in codec.get_tx_state()["bus"]["periodics"]}
        assert started["task_id"] not in tids


class TestListMessages:
    def test_returns_dbc_catalog(self, codec):
        result = codec.list_messages()
        assert result["bus"] == "busA"
        assert result["dbc_name"] == "test.dbc"
        names = {m["name"] for m in result["messages"]}
        assert {"DUT_Status", "DUT_Command", "DUT_Logging"} <= names
        status = next(m for m in result["messages"] if m["name"] == "DUT_Status")
        signal_names = {s["name"] for s in status["signals"]}
        assert "state" in signal_names
        assert "SOC_signal" in signal_names


class TestSendMessage:
    def test_dbc_encode_matches_cantools(self, codec, test_dbc):
        signals = {"state_request": 5}
        result = codec.send_message(message="DUT_Command", signals_json=json.dumps(signals))
        expected = bytes(test_dbc.get_message_by_name("DUT_Command").encode(signals))
        sent_msg: can.Message = codec.bus.send.call_args.args[0]
        assert bytes(sent_msg.data) == expected
        assert result["data_hex"] == expected.hex()

    def test_multiplexed_message_routes_mux_into_signals(self, codec, test_dbc):
        signals = {"logging_signal0": 1, "no_mux_logging_signal": 0}
        codec.send_message(message="DUT_Logging", signals_json=json.dumps(signals), mux="0")
        sent_msg: can.Message = codec.bus.send.call_args.args[0]
        expected = bytes(
            test_dbc.get_message_by_name("DUT_Logging").encode(
                {"logging_mux": 0, "logging_signal0": 1, "no_mux_logging_signal": 0}
            )
        )
        assert bytes(sent_msg.data) == expected

    def test_unknown_dbc_message_raises(self, codec):
        with pytest.raises(ValueError, match="unknown DBC message"):
            codec.send_message(message="NotAMessage", signals_json="{}")


class TestStartPeriodicMessage:
    def test_returns_task_id_and_replaced_semantics(self, codec):
        payload = json.dumps({"state_request": 0})
        r1 = codec.start_periodic_message(message="DUT_Command", signals_json=payload, period_ms=50)
        r2 = codec.start_periodic_message(message="DUT_Command", signals_json=payload, period_ms=50)
        assert r1["task_id"] == r2["task_id"]
        assert r1["replaced"] is False
        assert r2["replaced"] is True


class TestGetTxState:
    def test_snapshot_shape_matches_wire_contract(self, codec):
        snap = codec.get_tx_state()
        assert set(snap.keys()) >= {"captured_at_unix_ms", "extension", "bus"}
        assert snap["extension"]["id"] == "zeloscloud.zelos-extension-can"
        bus = snap["bus"]
        assert bus["name"] == "busA"
        assert bus["status"] == "active"
        assert "metrics" in bus
        for key in (
            "tx_errors",
            "tx_overflows",
            "messages_received",
            "messages_decoded",
            "unknown_messages",
        ):
            assert key in bus["metrics"]
        assert bus["dbc"]["name"] == "test.dbc"
        assert isinstance(bus["periodics"], list)

    def test_periodics_appear_in_snapshot(self, codec):
        started = codec.start_periodic_raw(can_id="0x300", data="aa", period_ms=50)
        tids = {p["task_id"] for p in codec.get_tx_state()["bus"]["periodics"]}
        assert started["task_id"] in tids


class TestDeriveBusStatus:
    """Tests the pure helper at its seam (memory: feedback_test_at_helper_seam)."""

    def test_returns_stopped_when_not_running(self):
        assert _derive_bus_status(False, object()) == "stopped"

    def test_returns_stopped_when_bus_is_none(self):
        assert _derive_bus_status(True, None) == "stopped"

    def test_returns_active_for_real_active_state(self):
        class FakeBus:
            state = can.BusState.ACTIVE

        assert _derive_bus_status(True, FakeBus()) == "active"

    def test_returns_error_for_error_state(self):
        class FakeBus:
            state = can.BusState.ERROR

        assert _derive_bus_status(True, FakeBus()) == "error"

    def test_falls_back_to_active_when_state_raises(self):
        class FakeBus:
            @property
            def state(self):
                raise NotImplementedError("virtual backend doesn't track state")

        assert _derive_bus_status(True, FakeBus()) == "active"

    def test_falls_back_to_active_when_state_isnt_bus_state(self):
        class FakeBus:
            state = "not-an-enum"  # mocked / unusual backend

        assert _derive_bus_status(True, FakeBus()) == "active"


class TestSendErrorCounter:
    def test_can_error_on_send_raw_increments_tx_errors_and_reraises(self, codec):
        codec.bus.send.side_effect = can.CanError("link down")
        assert codec.metrics.tx_errors == 0
        with pytest.raises(RuntimeError, match="send failed on bus 'busA'"):
            codec.send_raw(can_id="0x100", data="01")
        assert codec.metrics.tx_errors == 1

    def test_successful_send_does_not_touch_tx_errors(self, codec):
        codec.send_raw(can_id="0x100", data="01")
        assert codec.metrics.tx_errors == 0

    def test_tx_errors_surfaces_in_snapshot(self, codec):
        codec.bus.send.side_effect = can.CanError("bus off")
        with pytest.raises(RuntimeError):
            codec.send_raw(can_id="0x200", data="00")
        assert codec.get_tx_state()["bus"]["metrics"]["tx_errors"] == 1


class TestEncodePreview:
    def test_returns_encoded_bytes_without_calling_send(self, codec):
        result = codec.encode_preview(
            message="DUT_Command",
            signals_json=json.dumps({"state_request": 3}),
        )
        codec.bus.send.assert_not_called()
        assert result["message"] == "DUT_Command"
        assert "can_id_hex" in result
        assert "data_hex" in result
        assert isinstance(result["dlc"], int)
        assert result["dlc"] == len(bytes.fromhex(result["data_hex"]))

    def test_unknown_message_raises(self, codec):
        with pytest.raises(ValueError, match="unknown DBC message"):
            codec.encode_preview(message="DoesNotExist", signals_json="{}")


class TestEncodeHelper:
    def test_encode_dbc_returns_bytes_matching_cantools(self, test_dbc):
        msg = test_dbc.get_message_by_name("DUT_Command")
        signals = {"state_request": 3}
        out = _encode_dbc(msg, signals, mux_value=None)
        assert isinstance(out, bytes)
        assert out == bytes(msg.encode(signals))

    def test_encode_dbc_injects_mux_signal_when_not_in_payload(self, test_dbc):
        msg = test_dbc.get_message_by_name("DUT_Logging")
        out = _encode_dbc(
            msg,
            {"logging_signal0": 1, "no_mux_logging_signal": 0},
            mux_value=0,
        )
        assert out == bytes(
            msg.encode({"logging_mux": 0, "logging_signal0": 1, "no_mux_logging_signal": 0})
        )
