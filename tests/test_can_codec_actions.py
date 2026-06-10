"""Unit tests for the CAN codec's operations (the methods that back the
free-floating action surface in ``zelos_extension_can.actions``).

The on-wire action surface is a single global namespace:

    can/list_codecs
    can/get_tx_state           (codec=<bus>)
    can/send_message           (codec=<bus>, message=..., signals_json=..., mux=...)
    ...

These tests exercise the methods directly on a ``CanCodec`` instance with a
mocked python-can bus — that's the implementation layer the free functions in
``actions.py`` delegate to. Round-trip coverage of the actions module itself
lives in ``test_actions.py``.
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
    _describe_dbc_signal,
    _encode_dbc,
    _hash_dbc_file,
    _parse_can_id,
    _parse_data_hex,
    _parse_mux,
    _parse_signals_json,
    _scale_precision,
    _task_id,
    _validate_id_range,
    _value_table_for_trace,
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
    """list_messages is the lightweight summary call — names + identifiers
    only. Per-signal detail moved to describe_message (see below)."""

    def test_returns_summary_catalog(self, codec):
        result = codec.list_messages()
        assert result["bus"] == "busA"
        assert result["dbc_name"] == "test.dbc"
        names = {m["name"] for m in result["messages"]}
        assert {"DUT_Status", "DUT_Command", "DUT_Logging"} <= names
        status = next(m for m in result["messages"] if m["name"] == "DUT_Status")
        # Summary shape — identifiers only.
        assert set(status.keys()) == {"name", "can_id", "is_extended", "dlc", "cycle_time_ms"}
        assert "signals" not in status


class TestDescribeMessage:
    def test_returns_full_signal_detail(self, codec):
        result = codec.describe_message(message="DUT_Status")
        assert result["bus"] == "busA"
        assert result["dbc_name"] == "test.dbc"
        msg = result["message"]
        assert msg["name"] == "DUT_Status"
        # Summary fields are still here, plus signals.
        for key in ("can_id", "is_extended", "dlc", "cycle_time_ms", "signals"):
            assert key in msg
        signal_names = {s["name"] for s in msg["signals"]}
        assert "state" in signal_names
        assert "SOC_signal" in signal_names

    def test_unknown_message_raises(self, codec):
        with pytest.raises(ValueError, match="unknown DBC message"):
            codec.describe_message(message="DoesNotExist")


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
        # extension id/version/state intentionally NOT in this snapshot —
        # that info is canonical at extensions.list.
        assert set(snap.keys()) >= {"captured_at_unix_ms", "bus"}
        assert "extension" not in snap
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

    def test_snapshot_exposes_dbc_hash(self, codec):
        # Hash is a 16-char hex string so the webapp can key its
        # list_messages cache on it. Stable across snapshots of the same load.
        h1 = codec.get_tx_state()["bus"]["dbc"]["hash"]
        h2 = codec.get_tx_state()["bus"]["dbc"]["hash"]
        assert isinstance(h1, str) and len(h1) == 16
        assert h1 == h2


class TestDescribeDbcSignalValueTable:
    """value_table keys must be in PHYSICAL units, not raw — so a 12-bit
    unsigned signal with scale 0.001 and a VAL_ entry for raw 4095 surfaces
    as `{"4.095": "SNA"}` to the webapp, matching what the form sends."""

    def test_integer_scaled_signal_keeps_int_keys(self, test_dbc):
        # DUT_Command.state_request is integer-scaled — keys stay as raw ints.
        sig = next(s for s in test_dbc.get_message_by_name("DUT_Command").signals if s.choices)
        out = _describe_dbc_signal(sig)
        for k in out["value_table"]:
            assert k == str(int(k)), f"expected int key, got {k!r}"

    def test_floating_scaled_signal_uses_physical_keys(self, tmp_path):
        # Mini DBC with a scaled signal + VAL_ entry on raw 4095.
        dbc = tmp_path / "scaled.dbc"
        dbc.write_text(
            'VERSION ""\nNS_:\nBS_:\nBU_:\n'
            "BO_ 100 Cell: 8 BMS\n"
            ' SG_ voltage : 0|12@1+ (0.001,0) [0|5] "V" Receiver\n'
            'VAL_ 100 voltage 4095 "SNA";\n'
        )
        db = cantools.database.load_file(str(dbc))
        sig = next(s for s in db.get_message_by_name("Cell").signals if s.name == "voltage")
        out = _describe_dbc_signal(sig)
        assert out["value_table"] == {"4.095": "SNA"}

    def test_offset_signal_uses_physical_keys(self, tmp_path):
        # Temp signal with offset -40 — raw 215 → physical 175 °C SNA.
        dbc = tmp_path / "offset.dbc"
        dbc.write_text(
            'VERSION ""\nNS_:\nBS_:\nBU_:\n'
            "BO_ 100 Pack: 8 BMS\n"
            ' SG_ temp : 0|8@1+ (1,-40) [-40|125] "C" Receiver\n'
            'VAL_ 100 temp 215 "SNA";\n'
        )
        db = cantools.database.load_file(str(dbc))
        sig = next(s for s in db.get_message_by_name("Pack").signals if s.name == "temp")
        out = _describe_dbc_signal(sig)
        assert out["value_table"] == {"175": "SNA"}


class TestScalePrecision:
    """Decimal places implied by a signal's scale — used to trim fp64 noise
    from decoded values so they string-match value_table keys."""

    def test_thousandths_scale(self):
        assert _scale_precision(0.001) == 3

    def test_tenths_scale(self):
        assert _scale_precision(0.1) == 1

    def test_unity_scale(self):
        assert _scale_precision(1.0) == 0

    def test_integer_scale(self):
        # scale >= 1 has no fractional precision to preserve.
        assert _scale_precision(10.0) == 0
        assert _scale_precision(100.0) == 0

    def test_zero_or_negative_scale_defensive(self):
        assert _scale_precision(0.0) == 0
        assert _scale_precision(-0.1) == 0

    def test_tiny_scale(self):
        assert _scale_precision(1e-6) == 6


class TestConvertSignalsRounding:
    """End-to-end: a scaled signal whose decoded value lands at fp64 noise
    (e.g. 1234 * 0.001 = 1.2340000000000002) should be rounded to the
    scale's precision so the trace shows a clean number AND the webapp's
    value_table lookup hits."""

    def test_thousandths_rounding_clears_fp_noise(self, codec, test_dbc):
        msg = test_dbc.get_message_by_name("DUT_Logging")
        # Fabricate decoded dict with deliberate fp noise
        decoded = {"logging_mux": 0, "logging_signal0": 1.2340000000000002}
        out = codec._convert_signals(msg, decoded, base_only=False, mux_value=0)
        # logging_signal0 has scale=1 in test.dbc → no rounding, value passes through
        assert out["logging_signal0"] == 1.2340000000000002

    def test_rounding_applied_for_scaled_signal(self, codec):
        # Use BMS_CellVoltages-style synthetic via local helper
        import cantools

        db = cantools.database.load_string(
            'VERSION ""\nNS_:\nBS_:\nBU_:\n'
            "BO_ 100 X: 8 BMS\n"
            ' SG_ v : 0|12@1+ (0.001,0) [0|5] "V" Receiver\n'
        )
        msg = db.get_message_by_name("X")
        noisy = 1.2340000000000002
        out = codec._convert_signals(msg, {"v": noisy}, base_only=False, mux_value=None)
        # scale=0.001 → 3 decimal places → exact 1.234
        assert out["v"] == 1.234


class TestScaledSignalPrecisionEndToEnd:
    """Pins the 4.095 -> 4.09499979 regression. fp32 can't faithfully store
    decimal-like values; a 12-bit signal with scale 0.001 storing 4.095 as
    fp32 surfaces 4.094999790191650... ("4.09499979" when formatted), which
    breaks the value-table string lookup and gives users misleading trace
    values. The fix is Float64 trace storage for scaled signals.

    Splits the precision audit by stack layer so a future regression points
    at the exact layer that broke."""

    DBC_SOURCE = (
        'VERSION ""\nNS_:\nBS_:\nBU_:\n'
        "BO_ 100 X: 8 BMS\n"
        ' SG_ v : 0|12@1+ (0.001,0) [0|5] "V" Receiver\n'
        'VAL_ 100 v 4095 "SNA";\n'
    )

    def test_tx_pipeline_preserves_4_095(self):
        # Layer 1: JSON encode/decode (webapp -> agent) round-trips 4.095 cleanly.
        # JS's JSON.stringify uses "shortest unambiguous" formatting, Python's
        # json.loads round-trips fp64. So 4.095 in -> 4.095 out.
        roundtripped = json.loads(json.dumps({"v": 4.095}))
        assert roundtripped["v"] == 4.095

    def test_tx_cantools_encode_lands_on_raw_4095(self):
        # Layer 2: cantools encode of physical 4.095 (scale=0.001) produces
        # raw int 4095 on the wire. No precision loss here either.
        db = cantools.database.load_string(self.DBC_SOURCE)
        msg = db.get_message_by_name("X")
        raw = msg.encode({"v": 4.095}, strict=False)
        # Raw 4095 = 0x0FFF, little-endian in first 12 bits: byte0=0xFF, byte1=0x0F
        assert raw[0] == 0xFF
        assert raw[1] & 0x0F == 0x0F

    def test_rx_cantools_decode_returns_fp64_4_095(self):
        # Layer 3: cantools decode of raw 4095 returns a Python float that
        # round-trips to "4.095" via repr/.10g. The fp64 representation is
        # 4.0949999999999998 but format(4.095, '.10g') == '4.095'.
        db = cantools.database.load_string(self.DBC_SOURCE)
        msg = db.get_message_by_name("X")
        raw = bytes([0xFF, 0x0F, 0, 0, 0, 0, 0, 0])
        decoded = msg.decode(raw, decode_choices=False, scaling=True)
        assert format(decoded["v"], ".10g") == "4.095"

    def test_rx_convert_signals_rounds_to_scale_precision(self, codec):
        # Layer 4: _convert_signals rounds to scale's precision so the value
        # we emit to the trace is a clean fp64 4.095 (not 4.0949999...) and
        # the string-keyed value-table lookup in the UI succeeds.
        db = cantools.database.load_string(self.DBC_SOURCE)
        msg = db.get_message_by_name("X")
        out = codec._convert_signals(msg, {"v": 4.094999999999999}, base_only=False, mux_value=None)
        assert out["v"] == 4.095
        # Round-trip-safe string representation.
        assert format(out["v"], ".10g") == "4.095"

    def test_rx_trace_data_type_is_float64_for_scaled_signals(self):
        # Layer 5: schema setup picks Float64, NOT Float32. fp32's closest
        # rep of 4.095 is 4.0949997901916504 — formatting that with .10g
        # gives "4.09499979" (the user-visible regression). Float64 carries
        # enough decimal precision that the formatter rounds back to "4.095".
        db = cantools.database.load_string(self.DBC_SOURCE)
        sig = db.get_message_by_name("X").signals[0]
        # Pin the exact type so a future "smallest-type" optimization can't
        # silently regress this back to Float32.
        import zelos_sdk

        from zelos_extension_can.utils.schema_utils import cantools_signal_to_trace_type

        assert cantools_signal_to_trace_type(sig) == zelos_sdk.DataType.Float64


class TestValueTableForTrace:
    """zelos-sdk's add_value_table requires the keys to match the type the
    signal will be emitted as. Scaled signals are emitted as Float64; their
    value table must be float-keyed. Enum signals (identity conversion) are
    emitted as the smallest int that fits; their value table stays int-keyed."""

    def test_int_keyed_for_identity_conversion(self, test_dbc):
        sig = next(s for s in test_dbc.get_message_by_name("DUT_Command").signals if s.choices)
        out = _value_table_for_trace(sig)
        assert out is not None
        for k in out:
            assert isinstance(k, int), f"expected int key for identity-conv signal, got {type(k)}"

    def test_float_keyed_for_scaled_signal(self, tmp_path):
        dbc = tmp_path / "scaled.dbc"
        dbc.write_text(
            'VERSION ""\nNS_:\nBS_:\nBU_:\n'
            "BO_ 100 X: 8 BMS\n"
            ' SG_ v : 0|12@1+ (0.001,0) [0|5] "V" Receiver\n'
            'VAL_ 100 v 4095 "SNA";\n'
        )
        db = cantools.database.load_file(str(dbc))
        sig = db.get_message_by_name("X").signals[0]
        out = _value_table_for_trace(sig)
        assert out == {4.095: "SNA"}
        # Float key must equal what the rounding path emits, so the SDK's
        # lookup succeeds. Both are the same fp64 representation.
        from zelos_extension_can.codec import _scale_precision

        precision = _scale_precision(0.001)
        emitted = round(4095 * 0.001, precision)
        assert emitted in out  # dict lookup uses float equality


class TestHashDbcFile:
    def test_same_file_same_hash(self):
        assert _hash_dbc_file(DBC_PATH) == _hash_dbc_file(DBC_PATH)

    def test_different_contents_different_hash(self, tmp_path):
        a = tmp_path / "a.dbc"
        b = tmp_path / "b.dbc"
        a.write_bytes(b'VERSION "a"\n')
        b.write_bytes(b'VERSION "b"\n')
        assert _hash_dbc_file(a) != _hash_dbc_file(b)

    def test_returns_16_hex_chars(self):
        h = _hash_dbc_file(DBC_PATH)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


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
