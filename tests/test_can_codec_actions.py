"""Unit tests for the CAN codec's operations (the methods that back the
free-floating action surface in ``zelos_extension_can.actions``).

The on-wire action surface is a single global namespace:

    CAN/list_codecs
    CAN/get_tx_state           (codec=<bus>)
    CAN/send_message           (codec=<bus>, message=..., signals_json=..., mux=...)
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
from can.interfaces.virtual import VirtualBus

from zelos_extension_can.codec import CanCodec
from zelos_extension_can.dbc import (
    describe_dbc_signal,
    encode_dbc,
    hash_dbc_file,
    scale_precision,
    value_table_for_trace,
)
from zelos_extension_can.health import derive_bus_status
from zelos_extension_can.params import (
    parse_can_id,
    parse_data_hex,
    parse_mux,
    parse_signals_json,
    periodic_task_id,
    raw_slot,
    validate_id_range,
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
            "database_files": [str(DBC_PATH)],
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
        assert parse_can_id("0x100") == 0x100
        assert parse_can_id("0X1FF") == 0x1FF
        assert parse_can_id("100") == 0x100
        assert parse_can_id("  0x7ff  ") == 0x7FF

    def test_parse_data_hex_tolerates_spaces_and_commas(self):
        assert parse_data_hex("01 02 03 04") == b"\x01\x02\x03\x04"
        assert parse_data_hex("01,02,03") == b"\x01\x02\x03"
        assert parse_data_hex("") == b""

    def test_validate_id_range_standard_vs_extended(self):
        validate_id_range(0x7FF, is_extended=False)
        with pytest.raises(ValueError, match="out of range for standard"):
            validate_id_range(0x800, is_extended=False)
        validate_id_range(0x1FFFFFFF, is_extended=True)
        with pytest.raises(ValueError, match="out of range for extended"):
            validate_id_range(0x20000000, is_extended=True)

    def test_task_id_is_stable_across_payload_changes(self):
        # Same slot for the same CAN ID + frame kind on the same bus →
        # starting the periodic twice replaces the prior slot.
        a = periodic_task_id(raw_slot(0x100, is_extended=False), mux="raw")
        b = periodic_task_id(raw_slot(0x100, is_extended=False), mux="raw")
        assert a == b == "0x100:std:raw"

    def test_task_id_distinguishes_std_vs_ext(self):
        # Standard vs extended frames with the same numeric ID stay separate slots.
        assert raw_slot(0x100, False) != raw_slot(0x100, True)

    def test_parse_mux_returns_none_int_or_label(self):
        assert parse_mux("") is None
        assert parse_mux("  ") is None
        assert parse_mux("3") == 3
        assert parse_mux("0x2") == 2
        assert parse_mux("Reverse") == "Reverse"

    def test_parse_signals_json_rejects_non_object(self):
        assert parse_signals_json('{"Speed": 50}') == {"Speed": 50}
        with pytest.raises(ValueError, match="JSON object"):
            parse_signals_json("[1, 2]")
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_signals_json("{not json}")


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
        # Summary shape — key + identifiers plus the file it came from.
        assert set(status.keys()) == {
            "key",
            "name",
            "can_id",
            "is_extended",
            "dlc",
            "cycle_time_ms",
            "database",
        }
        assert status["key"] == f"{status['can_id']:04x}_DUT_Status"
        assert status["database"] == "test.dbc"
        assert "signals" not in status
        assert result["dbcs"] == ["test.dbc"]

    def test_every_definition_is_listed_under_its_own_key(self, codec):
        """test.dbc defines Duplicate_Message at two ids. Both are listed, each
        addressable by key; the bare name is not."""
        messages = codec.list_messages()["messages"]
        dups = [m for m in messages if m["name"] == "Duplicate_Message"]

        assert [m["key"] for m in dups] == ["0190_Duplicate_Message", "01f4_Duplicate_Message"]
        assert codec.catalog.resolve("0190_Duplicate_Message").frame_id == 400
        with pytest.raises(ValueError, match="defined at several ids"):
            codec.catalog.resolve("Duplicate_Message")


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
        out = describe_dbc_signal(sig)
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
        out = describe_dbc_signal(sig)
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
        out = describe_dbc_signal(sig)
        assert out["value_table"] == {"175": "SNA"}


class TestScalePrecision:
    """Decimal places implied by a signal's scale — used to trim fp64 noise
    from decoded values so they string-match value_table keys."""

    def test_thousandths_scale(self):
        assert scale_precision(0.001) == 3

    def test_tenths_scale(self):
        assert scale_precision(0.1) == 1

    def test_unity_scale(self):
        assert scale_precision(1.0) == 0

    def test_integer_scale(self):
        # scale >= 1 has no fractional precision to preserve.
        assert scale_precision(10.0) == 0
        assert scale_precision(100.0) == 0

    def test_zero_or_negative_scale_defensive(self):
        assert scale_precision(0.0) == 0
        assert scale_precision(-0.1) == 0

    def test_tiny_scale(self):
        assert scale_precision(1e-6) == 6


class TestConvertSignalsRounding:
    """End-to-end: a scaled signal whose decoded value lands at fp64 noise
    (e.g. 1234 * 0.001 = 1.2340000000000002) should be rounded to the
    scale's precision so the trace shows a clean number AND the webapp's
    value_table lookup hits."""

    def test_thousandths_rounding_clears_fp_noise(self, codec, test_dbc):
        msg = test_dbc.get_message_by_name("DUT_Logging")
        # Fabricate decoded dict with deliberate fp noise
        decoded = {"logging_mux": 0, "logging_signal0": 1.2340000000000002}
        out = codec.decoder._convert_signals(msg, decoded, base_only=False, mux_value=0)
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
        out = codec.decoder._convert_signals(msg, {"v": noisy}, base_only=False, mux_value=None)
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
        out = codec.decoder._convert_signals(
            msg, {"v": 4.094999999999999}, base_only=False, mux_value=None
        )
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
        out = value_table_for_trace(sig)
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
        out = value_table_for_trace(sig)
        assert out == {4.095: "SNA"}
        # Float key must equal what the rounding path emits, so the SDK's
        # lookup succeeds. Both are the same fp64 representation.
        from zelos_extension_can.dbc import scale_precision

        precision = scale_precision(0.001)
        emitted = round(4095 * 0.001, precision)
        assert emitted in out  # dict lookup uses float equality


class TestHashDbcFile:
    def test_same_file_same_hash(self):
        assert hash_dbc_file(DBC_PATH) == hash_dbc_file(DBC_PATH)

    def test_different_contents_different_hash(self, tmp_path):
        a = tmp_path / "a.dbc"
        b = tmp_path / "b.dbc"
        a.write_bytes(b'VERSION "a"\n')
        b.write_bytes(b'VERSION "b"\n')
        assert hash_dbc_file(a) != hash_dbc_file(b)

    def test_returns_16_hex_chars(self):
        h = hash_dbc_file(DBC_PATH)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


class TestDeriveBusStatus:
    """Tests the pure helper at its seam (memory: feedback_test_at_helper_seam)."""

    def test_returns_stopped_when_not_running(self):
        assert derive_bus_status(False, object()) == "stopped"

    def test_returns_stopped_when_bus_is_none(self):
        assert derive_bus_status(True, None) == "stopped"

    def test_returns_active_for_real_active_state(self):
        class FakeBus:
            state = can.BusState.ACTIVE

        assert derive_bus_status(True, FakeBus()) == "active"

    def test_returns_error_for_error_state(self):
        class FakeBus:
            state = can.BusState.ERROR

        assert derive_bus_status(True, FakeBus()) == "error"

    def test_falls_back_to_active_when_state_raises(self):
        class FakeBus:
            @property
            def state(self):
                raise NotImplementedError("virtual backend doesn't track state")

        assert derive_bus_status(True, FakeBus()) == "active"

    def test_falls_back_to_active_when_state_isnt_bus_state(self):
        class FakeBus:
            state = "not-an-enum"  # mocked / unusual backend

        assert derive_bus_status(True, FakeBus()) == "active"


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
        out = encode_dbc(msg, signals, mux_value=None)
        assert isinstance(out, bytes)
        assert out == bytes(msg.encode(signals))

    def test_encode_dbc_names_missing_signals(self, test_dbc):
        msg = test_dbc.get_message_by_name("DUT_Command")
        with pytest.raises(ValueError, match="needs signals: state_request"):
            encode_dbc(msg, {}, mux_value=None)

    def test_encode_dbc_injects_mux_signal_when_not_in_payload(self, test_dbc):
        msg = test_dbc.get_message_by_name("DUT_Logging")
        out = encode_dbc(
            msg,
            {"logging_signal0": 1, "no_mux_logging_signal": 0},
            mux_value=0,
        )
        assert out == bytes(
            msg.encode({"logging_mux": 0, "logging_signal0": 1, "no_mux_logging_signal": 0})
        )


# ─── Bus health: what the controller reports, not the mode the bus was opened in ──


class _FakePcan:
    """A PcanBus whose status() is fixed, without opening hardware."""

    @staticmethod
    def make(code: int | Exception):
        from can.interfaces.pcan import PcanBus

        class Fake(PcanBus):
            def __init__(self):  # noqa: D401 - no hardware
                pass

            def status(self):
                if isinstance(code, Exception):
                    raise code
                return code

        return Fake()


class TestPcanControllerState:
    @pytest.mark.parametrize(
        ("code_name", "state"),
        [
            ("PCAN_ERROR_OK", "ok"),
            ("PCAN_ERROR_BUSLIGHT", "warning"),
            ("PCAN_ERROR_BUSHEAVY", "warning"),
            ("PCAN_ERROR_BUSPASSIVE", "passive"),
            ("PCAN_ERROR_BUSOFF", "bus_off"),
        ],
    )
    def test_maps_the_driver_status(self, code_name, state):
        from can.interfaces.pcan import basic as pcan

        from zelos_extension_can.pcan import pcan_controller_state

        assert pcan_controller_state(_FakePcan.make(getattr(pcan, code_name))) == state

    def test_a_full_transmit_queue_alongside_warning_still_reads_warning(self):
        from can.interfaces.pcan import basic as pcan

        from zelos_extension_can.pcan import pcan_controller_state

        code = pcan.PCAN_ERROR_BUSHEAVY | pcan.PCAN_ERROR_QXMTFULL
        assert pcan_controller_state(_FakePcan.make(code)) == "warning"

    @pytest.mark.parametrize(
        "code_name",
        [
            "PCAN_ERROR_ILLHW",
            "PCAN_ERROR_ILLNET",
            "PCAN_ERROR_ILLCLIENT",
            "PCAN_ERROR_ILLHANDLE",
            "PCAN_ERROR_NODRIVER",
            "PCAN_ERROR_REGTEST",
            "PCAN_ERROR_INITIALIZE",
        ],
    )
    def test_a_vanished_adapter_is_unavailable_not_ok(self, code_name):
        """Pulling the USB cable makes status() answer an invalid-handle code."""
        from can.interfaces.pcan import basic as pcan

        from zelos_extension_can.pcan import pcan_controller_state

        assert pcan_controller_state(_FakePcan.make(getattr(pcan, code_name))) == "unavailable"

    @pytest.mark.parametrize(
        "code_name",
        ["PCAN_ERROR_CAUTION", "PCAN_ERROR_ILLDATA", "PCAN_ERROR_UNKNOWN", "PCAN_ERROR_HWINUSE"],
    )
    def test_other_codes_leave_the_state_to_the_bus_bits(self, code_name):
        """Only handle, driver and hardware codes mean the adapter is gone."""
        from can.interfaces.pcan import basic as pcan

        from zelos_extension_can.pcan import pcan_controller_state

        code = getattr(pcan, code_name)
        assert pcan_controller_state(_FakePcan.make(code)) == "ok"
        assert pcan_controller_state(_FakePcan.make(code | pcan.PCAN_ERROR_BUSHEAVY)) == "warning"

    def test_a_status_that_raises_is_unavailable(self):
        from zelos_extension_can.pcan import pcan_controller_state

        gone = can.CanOperationError("The value of a handle is invalid")
        assert pcan_controller_state(_FakePcan.make(gone)) == "unavailable"

    def test_any_other_bus_reports_nothing(self):
        from zelos_extension_can.pcan import pcan_controller_state

        assert pcan_controller_state(object()) is None


def _ip_reply(monkeypatch, *, stdout="", stderr="", returncode=0, calls=None):
    import subprocess

    def run(args, **_kwargs):
        if calls is not None:
            calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("zelos_extension_can.socketcan.subprocess.run", run)


def _link_json(state, flags=("NOARP", "UP", "ECHO"), bitrate=250_000, restart_ms=0, ifindex=4):
    info = {"state": state, "berr_counter": {"tx": 128, "rx": 3}, "restart_ms": restart_ms}
    if bitrate:
        info["bittiming"] = {"bitrate": bitrate}
    return json.dumps(
        [
            {
                "ifindex": ifindex,
                "ifname": "can0",
                "flags": list(flags),
                "linkinfo": {"info_kind": "can", "info_data": info},
            }
        ]
    )


_DOWN = ("NOARP", "ECHO")

#: `ip -details -json link show can2` on Linux 6.8 for a PCAN-USB Pro FD channel
#: set up with `bitrate 500000 sample-point 0.8 dbitrate 2000000 dsample-point 0.75
#: fd on listen-only on restart-ms 100`.
_FD_LINK = json.dumps(
    [
        {
            "ifindex": 5,
            "ifname": "can2",
            "flags": ["NOARP", "UP", "LOWER_UP", "ECHO"],
            "linkinfo": {
                "info_kind": "can",
                "info_data": {
                    "ctrlmode": ["LISTEN-ONLY", "FD"],
                    "state": "ERROR-ACTIVE",
                    "berr_counter": {"tx": 0, "rx": 0},
                    "restart_ms": 100,
                    "bittiming": {"bitrate": 500000, "sample_point": 0.8, "tq": 12},
                    "data_bittiming": {"bitrate": 2000000, "sample_point": 0.75, "tq": 12},
                    "clock": 80000000,
                },
            },
        }
    ]
)
_FD_SETTINGS = [
    "bitrate",
    "500000",
    "sample-point",
    "0.8",
    "dbitrate",
    "2000000",
    "dsample-point",
    "0.75",
    "listen-only",
    "on",
    "fd",
    "on",
    "restart-ms",
    "100",
]


class _Ip:
    """A fake `ip`: `show` answers with `link` (None: the interface is gone),
    and every `set` is recorded and answered as configured."""

    def __init__(self, monkeypatch, link, *, set_returncode=0, set_raises=None):
        self.link = link
        self.set_returncode = set_returncode
        self.set_raises = set_raises
        self.sets: list[list[str]] = []
        monkeypatch.setattr("zelos_extension_can.socketcan.subprocess.run", self.run)

    def run(self, args, **_kwargs):
        import subprocess

        if args[:3] == ["ip", "link", "set"]:
            self.sets.append(args[5:])
            if self.set_raises is not None:
                raise self.set_raises
            stderr = "RTNETLINK answers: Operation not permitted" if self.set_returncode else ""
            return subprocess.CompletedProcess(args, self.set_returncode, stdout="", stderr=stderr)
        if self.link is None:
            stderr = 'Device "can0" does not exist.'
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=stderr)
        return subprocess.CompletedProcess(args, 0, stdout=self.link, stderr="")


def _as_socketcan(codec, interface="socketcan", channel="can0"):
    """Make a test codec read its health as a SocketCAN bus does."""
    from zelos_extension_can.health import BusHealth
    from zelos_extension_can.socketcan import SocketcanSupervisor

    codec.config["interface"] = interface
    codec.config["channel"] = channel
    codec.health = BusHealth(interface)
    codec._supervisor = SocketcanSupervisor(channel, codec.bus_name)
    return codec


class TestSocketcanLink:
    @pytest.mark.parametrize(
        ("kernel", "state"),
        [
            ("ERROR-ACTIVE", "ok"),
            ("ERROR-WARNING", "warning"),
            ("ERROR-PASSIVE", "passive"),
            ("BUS-OFF", "bus_off"),
        ],
    )
    def test_reads_state_counters_and_settings(self, monkeypatch, kernel, state):
        from zelos_extension_can.socketcan import socketcan_link

        _ip_reply(monkeypatch, stdout=_link_json(kernel))
        link = socketcan_link("can0")
        assert (link.exists, link.ifindex, link.up, link.state) == (True, 4, True, state)
        assert link.counters == {"tx_error_count": 128, "rx_error_count": 3}
        assert link.settings == ["bitrate", "250000"]

    def test_reads_an_fd_controllers_settings(self, monkeypatch):
        from zelos_extension_can.socketcan import socketcan_link

        _ip_reply(monkeypatch, stdout=_FD_LINK)
        assert socketcan_link("can2").settings == _FD_SETTINGS

    def test_switchable_termination_is_kept(self, monkeypatch):
        from zelos_extension_can.socketcan import socketcan_settings

        info = {"bittiming": {"bitrate": 500000}, "termination": 120}
        assert socketcan_settings(info) == ["bitrate", "500000", "termination", "120"]

    def test_a_freshly_plugged_adapter_has_no_settings(self, monkeypatch):
        from zelos_extension_can.socketcan import socketcan_link

        _ip_reply(monkeypatch, stdout=_link_json("STOPPED", flags=_DOWN, bitrate=None))
        link = socketcan_link("can0")
        assert (link.up, link.settings) == (False, [])

    def test_a_vanished_interface_does_not_exist(self, monkeypatch):
        from zelos_extension_can.socketcan import socketcan_link

        _ip_reply(monkeypatch, returncode=1, stderr='Device "can0" does not exist.')
        assert socketcan_link("can0").exists is False

    def test_vcan_reports_no_state(self, monkeypatch):
        from zelos_extension_can.socketcan import socketcan_link

        vcan = [{"ifname": "vcan0", "flags": ["UP"], "linkinfo": {"info_kind": "vcan"}}]
        _ip_reply(monkeypatch, stdout=json.dumps(vcan))
        link = socketcan_link("vcan0")
        assert (link.state, link.settings) == (None, [])

    def test_a_missing_ip_reports_nothing(self, monkeypatch):
        from zelos_extension_can.socketcan import socketcan_link

        def run(*_args, **_kwargs):
            raise FileNotFoundError("ip")

        monkeypatch.setattr("zelos_extension_can.socketcan.subprocess.run", run)
        assert socketcan_link("can0") is None


class TestCanAdminLinks:
    @pytest.mark.parametrize(
        ("cap_eff", "allowed"),
        [
            ("000001ffffffffff", True),  # root
            ("0000000000001000", True),  # CAP_NET_ADMIN alone
            ("0000000000000000", False),
            ("00000000a80425fb", False),  # a container's default set, without it
        ],
    )
    def test_reads_cap_net_admin(self, tmp_path, cap_eff, allowed):
        from zelos_extension_can.socketcan import can_admin_links

        status = tmp_path / "status"
        status.write_text(f"Name:\tpython\nCapPrm:\t0\nCapEff:\t{cap_eff}\n")
        assert can_admin_links(status) is allowed

    def test_no_proc_means_no(self, tmp_path):
        from zelos_extension_can.socketcan import can_admin_links

        assert can_admin_links(tmp_path / "missing") is False


class TestSocketcanHealth:
    def test_gone_says_check_usb(self):
        from zelos_extension_can.socketcan import SocketcanLink, socketcan_health

        assert socketcan_health("can0", SocketcanLink(exists=False), []) == (
            "unavailable",
            "can0 is gone. Check its USB connection.",
        )

    def test_down_names_the_command_with_the_saved_settings(self):
        from zelos_extension_can.socketcan import SocketcanLink, socketcan_health

        settings = ["bitrate", "500000", "dbitrate", "2000000", "fd", "on"]
        link = SocketcanLink(exists=True, up=False)
        state, detail = socketcan_health("can0", link, settings)
        assert state == "unavailable"
        assert detail.endswith(
            "sudo ip link set can0 up type can bitrate 500000 dbitrate 2000000 fd on"
        )

    def test_down_with_nothing_known_names_a_placeholder(self):
        from zelos_extension_can.socketcan import SocketcanLink, socketcan_health

        _, detail = socketcan_health("can0", SocketcanLink(exists=True, up=False), [])
        assert detail.endswith("sudo ip link set can0 up type can bitrate BITRATE")

    def test_bus_off_without_restart_names_the_fix(self):
        from zelos_extension_can.socketcan import SocketcanLink, socketcan_health

        link = SocketcanLink(exists=True, up=True, state="bus_off", restart_ms=0)
        state, detail = socketcan_health("can1", link, [])
        assert state == "bus_off"
        assert "sudo ip link set can1 type can restart-ms 100" in detail

    def test_bus_off_the_kernel_will_restart_needs_no_fix(self):
        from zelos_extension_can.health import HEALTH_DETAIL
        from zelos_extension_can.socketcan import SocketcanLink, socketcan_health

        link = SocketcanLink(exists=True, up=True, state="bus_off", restart_ms=100)
        assert socketcan_health("can1", link, []) == ("bus_off", HEALTH_DETAIL["bus_off"])


class TestSuperviseSocketcan:
    """With CAP_NET_ADMIN a bus-off controller is restarted and a replugged
    interface comes back as it was; nothing done to an interface by hand is undone."""

    @pytest.fixture
    def sock(self, monkeypatch):
        from zelos_extension_can.socketcan import SocketcanSupervisor

        monkeypatch.setattr("zelos_extension_can.socketcan.can_admin_links", lambda: True)
        return SocketcanSupervisor("can0", "busA")

    def test_a_bus_off_controller_is_restarted_every_look_and_logged_once_a_minute(
        self, sock, monkeypatch, caplog
    ):
        import logging

        clock = [1000.0]
        monkeypatch.setattr("zelos_extension_can.codec.time.monotonic", lambda: clock[0])
        ip = _Ip(monkeypatch, _link_json("BUS-OFF"))
        with caplog.at_level(logging.INFO, logger="zelos_extension_can.codec"):
            for second in (0, 5, 10, 61):
                clock[0] = 1000.0 + second
                assert sock.look() is False
        assert ip.sets == [["type", "can", "restart"]] * 4
        assert [r.getMessage() for r in caplog.records if "bus-off" in r.getMessage()] == [
            "[busA] restarted can0 after bus-off",
            "[busA] restarted can0 after bus-off (3 restarts since the last report)",
        ]

    def test_a_replugged_interface_comes_back_with_its_settings(self, sock, monkeypatch):
        ip = _Ip(monkeypatch, _FD_LINK)
        assert sock.look() is False
        ip.link = None
        assert sock.look() is False
        ip.link = _link_json("STOPPED", flags=_DOWN, bitrate=None, ifindex=9)
        assert sock.look() is True
        assert ip.sets == [["up", "type", "can", *_FD_SETTINGS]]

    def test_a_replug_between_two_looks_is_still_caught(self, sock, monkeypatch):
        """Unplugged and plugged back within one poll: never seen missing."""
        ip = _Ip(monkeypatch, _link_json("ERROR-ACTIVE"))
        sock.look()
        ip.link = _link_json("STOPPED", flags=_DOWN, bitrate=None, ifindex=7)
        assert sock.look() is True
        assert ip.sets == [["up", "type", "can", "bitrate", "250000"]]

    def test_the_interface_is_known_from_the_moment_it_opens(self, sock, monkeypatch):
        """Unplugged before the first poll: the interface and its settings were
        recorded when the bus opened."""
        ip = _Ip(monkeypatch, _link_json("ERROR-ACTIVE"))
        sock.note_link()
        ip.link = _link_json("STOPPED", flags=_DOWN, bitrate=None, ifindex=6)
        assert sock.look() is True
        assert ip.sets == [["up", "type", "can", "bitrate", "250000"]]

    def test_a_replugged_interface_already_up_is_reopened_as_it_is(self, sock, monkeypatch):
        ip = _Ip(monkeypatch, _link_json("ERROR-ACTIVE"))
        sock.look()
        ip.link = _link_json("ERROR-ACTIVE", bitrate=500_000, ifindex=8)
        assert sock.look() is True
        assert ip.sets == []

    def test_an_interface_taken_down_by_hand_stays_down(self, sock, monkeypatch):
        ip = _Ip(monkeypatch, _link_json("ERROR-ACTIVE"))
        sock.look()
        ip.link = _link_json("STOPPED", flags=_DOWN)
        assert sock.look() is False
        assert ip.sets == []

    def test_a_bitrate_changed_by_hand_is_kept_and_restored_after_a_replug(self, sock, monkeypatch):
        ip = _Ip(monkeypatch, _link_json("ERROR-ACTIVE"))
        sock.look()
        ip.link = _link_json("STOPPED", flags=_DOWN, bitrate=500_000)
        assert sock.look() is False
        ip.link = _link_json("ERROR-ACTIVE", bitrate=500_000)
        assert sock.look() is False
        assert ip.sets == []
        ip.link = _link_json("STOPPED", flags=_DOWN, bitrate=None, ifindex=9)
        assert sock.look() is True
        assert ip.sets == [["up", "type", "can", "bitrate", "500000"]]

    def test_a_failed_bring_up_is_retried_on_the_next_look(self, sock, monkeypatch):
        ip = _Ip(monkeypatch, _link_json("ERROR-ACTIVE"), set_returncode=2)
        sock.look()
        ip.link = _link_json("STOPPED", flags=_DOWN, bitrate=None, ifindex=9)
        assert sock.look() is False
        assert sock.look() is False
        assert len(ip.sets) == 2

    def test_a_bring_up_that_keeps_failing_is_logged_once_a_minute(self, sock, monkeypatch, caplog):
        import logging

        clock = [1000.0]
        monkeypatch.setattr("zelos_extension_can.codec.time.monotonic", lambda: clock[0])
        ip = _Ip(monkeypatch, _FD_LINK, set_returncode=2)
        sock.look()
        ip.link = _link_json("STOPPED", flags=_DOWN, bitrate=None, ifindex=9)
        with caplog.at_level(logging.WARNING, logger="zelos_extension_can.codec"):
            for second in (0, 5, 10, 61):
                clock[0] = 1000.0 + second
                sock.look()
        assert len(ip.sets) == 4
        assert len([r for r in caplog.records if "Could not recover can0" in r.getMessage()]) == 2

    def test_an_ip_that_hangs_does_not_raise(self, sock, monkeypatch):
        import subprocess

        hang = subprocess.TimeoutExpired(["ip"], 2.0)
        ip = _Ip(monkeypatch, _link_json("BUS-OFF"), set_raises=hang)
        assert sock.look() is False
        assert ip.sets == [["type", "can", "restart"]]

    def test_without_cap_net_admin_nothing_is_changed(self, sock, monkeypatch):
        monkeypatch.setattr("zelos_extension_can.socketcan.can_admin_links", lambda: False)
        ip = _Ip(monkeypatch, _link_json("BUS-OFF"))
        sock.look()
        ip.link = _link_json("STOPPED", flags=_DOWN, bitrate=None, ifindex=9)
        assert sock.look() is False
        ip.link = _link_json("ERROR-ACTIVE", ifindex=9)
        assert sock.look() is True, "brought up by hand: reopen"
        assert ip.sets == []


class TestReopenNative:
    """Reopening the native codec on a replugged interface."""

    @staticmethod
    def _native(received: int):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        native = MagicMock()
        native.metrics.return_value = SimpleNamespace(
            messages_received=received,
            messages_decoded=received - 10 if received > 10 else received,
            unknown_messages=1,
            tx_errors=2,
            tx_overflows=0,
        )
        return native

    def test_counts_never_drop_and_periodics_come_back(self, codec, monkeypatch):
        from unittest.mock import MagicMock

        old, new = self._native(40), self._native(5)
        codec._native = old
        codec.periodics.specs["t"] = (can.Message(arbitration_id=0x3AA, data=b"\xaa"), 0.1, "raw")
        spawned, seen = [], []
        monkeypatch.setattr(codec.periodics, "start", lambda _bus, tid, *a: spawned.append(tid))

        def start_native():
            seen.append(codec._native_rx_counts()["messages_received"])
            codec._native, codec.bus = new, MagicMock()

        monkeypatch.setattr(codec, "_start_native", start_native)
        before = codec._native_rx_counts()["messages_received"]
        codec._restart_native()
        after = codec._native_rx_counts()["messages_received"]
        assert old.stop.called
        assert (before, seen, after) == (40, [40], 45)
        assert codec._native_tx_counts() == {"tx_errors": 4, "tx_overflows": 0}
        assert spawned == ["t"]

    def test_a_failed_reopen_reads_reconnecting_and_retries(self, codec, monkeypatch):
        from unittest.mock import MagicMock

        codec._native = self._native(40)
        attempts = []

        def start_native():
            attempts.append(1)
            if len(attempts) == 1:
                raise can.CanOperationError("No such device")
            codec._native, codec.bus = self._native(0), MagicMock()

        monkeypatch.setattr(codec, "_start_native", start_native)
        with pytest.raises(can.CanOperationError):
            codec._restart_native()
        assert (codec.running, codec.bus, codec._native) == (True, None, None)
        bus = codec.get_tx_state()["bus"]
        assert (bus["status"], bus["health"]["detail"]) == (
            "error",
            "The bus is down; reconnecting to the adapter.",
        )
        codec._restart_native()
        assert codec.bus is not None
        assert codec._native_rx_counts()["messages_received"] == 40

    def test_stop_during_a_reopen_leaves_the_bus_stopped(self, codec, monkeypatch):
        import threading
        import time as _time
        from unittest.mock import MagicMock

        opening, release = threading.Event(), threading.Event()

        def slow_start_native():
            opening.set()
            release.wait(5)
            codec._native, codec.bus = self._native(0), MagicMock()
            codec.running = True

        monkeypatch.setattr(codec, "_start_native", slow_start_native)
        reopen = threading.Thread(target=codec._restart_native)
        reopen.start()
        assert opening.wait(5)
        stopper = threading.Thread(target=codec.stop)
        stopper.start()
        deadline = _time.monotonic() + 5
        while codec.running and _time.monotonic() < deadline:
            _time.sleep(0.001)
        assert not codec.running, "stop() never began"
        release.set()
        reopen.join(5)
        stopper.join(5)
        assert (codec.running, codec.bus, codec._native) == (False, None, None)

    def test_one_periodic_failing_to_restart_spares_the_rest(self, codec, monkeypatch):
        for tid in ("a", "b", "c"):
            codec.periodics.specs[tid] = (can.Message(arbitration_id=0x10), 0.1, "raw")
        started = []

        def spawn(_bus, tid, *_args):
            if tid == "b":
                raise can.CanOperationError("Failed to set up the periodic")
            started.append(tid)

        monkeypatch.setattr(codec.periodics, "start", spawn)
        codec.periodics.rearm(codec.bus)
        assert started == ["a", "c"]
        assert "b" in codec.periodics.specs, "kept for the next reopen"

    def test_a_replugged_interface_names_the_settings_it_last_had(self, codec, monkeypatch):
        _Ip(monkeypatch, _link_json("STOPPED", flags=_DOWN, bitrate=None, ifindex=9))
        _as_socketcan(codec)
        codec._supervisor.settings = _FD_SETTINGS
        detail = codec.get_tx_state()["bus"]["health"]["detail"]
        assert detail == "can0 is down. Bring it up: sudo ip link set can0 up type can " + " ".join(
            _FD_SETTINGS
        )

    def test_a_reopen_after_stop_opens_nothing(self, codec, monkeypatch):
        opened = []
        monkeypatch.setattr(codec, "_start_native", lambda: opened.append(1))
        codec.stop()
        codec._restart_native()
        assert opened == []


class TestSupervisionLoops:
    """One bus failing to recover must not end the others: `_run_codecs_async`
    stops every bus when any loop raises."""

    @staticmethod
    def _fast(monkeypatch):
        import asyncio

        real_sleep = asyncio.sleep

        async def no_wait(_seconds):
            await real_sleep(0)

        monkeypatch.setattr("zelos_extension_can.codec.asyncio.sleep", no_wait)

    @pytest.mark.parametrize("failing", ["supervise", "reopen"])
    def test_the_native_loop_survives_a_failed_recovery(self, codec, monkeypatch, failing):
        import asyncio
        import subprocess
        from types import SimpleNamespace

        self._fast(monkeypatch)
        codec._use_native = True
        looks, reopens = [], []

        def supervise():
            looks.append(1)
            if len(looks) == 3:
                codec.running = False
            if failing == "supervise" and len(looks) == 1:
                raise subprocess.TimeoutExpired(["ip"], 1.0)
            return True

        def reopen():
            reopens.append(1)
            if failing == "reopen" and len(reopens) == 1:
                raise can.CanOperationError("No such device")

        monkeypatch.setattr(codec, "_supervisor", SimpleNamespace(look=supervise))
        monkeypatch.setattr(codec, "_restart_native", reopen)
        asyncio.run(codec._run_async())
        assert len(looks) == 3
        assert len(reopens) == (2 if failing == "supervise" else 3)

    def test_the_socketcan_py_loop_survives_a_failed_look(self, codec, monkeypatch):
        import asyncio
        import subprocess
        from types import SimpleNamespace

        self._fast(monkeypatch)
        _as_socketcan(codec, "socketcan-py")
        looks, reconnects = [], []

        def supervise():
            looks.append(1)
            if len(looks) == 1:
                raise subprocess.TimeoutExpired(["ip"], 1.0)
            codec.running = False
            return True

        async def reconnect():
            reconnects.append(1)

        monkeypatch.setattr(codec, "_supervisor", SimpleNamespace(look=supervise))
        monkeypatch.setattr(codec, "_handle_reconnection", reconnect)
        monkeypatch.setattr(codec, "_start_notifier", lambda: None)
        monkeypatch.setattr(codec, "_check_notifier_health", lambda _n: True)
        monkeypatch.setattr(codec, "_check_bus_health", lambda: True)
        asyncio.run(codec._run_async())
        assert (len(looks), len(reconnects)) == (2, 1)


class TestSocketcanErrorFrame:
    @pytest.mark.parametrize(
        ("error_class", "controller", "state", "says"),
        [
            (0x20, 0, "warning", "No node acknowledged"),
            (0x24, 0x20, "passive", "No node acknowledged"),
            (0x04, 0x20, "passive", "most frames are failing"),
            (0x204, 0x08, "warning", "error counters are high"),
            (0x40, 0, "bus_off", "sends nothing"),
            (0x08, 0, "warning", "saw errors on the wire"),
            (0x80, 0, "warning", "saw errors on the wire"),
            (0x04, 0x01, "warning", "buffer overflowed"),
            (0x204, 0x40, "ok", None),
            (0x100, 0, "ok", None),
        ],
    )
    def test_reads_linux_error_classes(self, error_class, controller, state, says):
        from zelos_extension_can.health import socketcan_error_frame

        frame = can.Message(
            arbitration_id=error_class,
            is_error_frame=True,
            data=bytes([0, controller, 0, 0, 0, 0, 0, 0]),
        )
        got_state, detail = socketcan_error_frame(frame)
        assert got_state == state
        assert detail is None if says is None else says in detail

    @pytest.mark.parametrize("error_class", [0x02, 0x200], ids=["lost_arbitration", "counters"])
    def test_frames_that_say_nothing_of_health_are_ignored(self, error_class):
        from zelos_extension_can.health import socketcan_error_frame

        frame = can.Message(arbitration_id=error_class, is_error_frame=True, data=bytes(8))
        assert socketcan_error_frame(frame) is None


class TestBusHealthInSnapshot:
    def test_a_healthy_bus_is_active(self, codec):
        bus = codec.get_tx_state()["bus"]
        assert bus["status"] == "active"
        assert bus["health"] == {"state": "unknown", "detail": None}

    def test_high_error_counters_read_warning_with_why(self, codec, monkeypatch):
        monkeypatch.setattr(
            "zelos_extension_can.codec.pcan_controller_state", lambda _bus: "warning"
        )
        bus = codec.get_tx_state()["bus"]
        assert bus["status"] == "warning"
        assert bus["health"]["state"] == "warning"
        assert "frames have been failing on the wire" in bus["health"]["detail"]

    @pytest.mark.parametrize("state", ["passive", "bus_off"])
    def test_passive_or_bus_off_is_an_error(self, codec, monkeypatch, state):
        monkeypatch.setattr("zelos_extension_can.codec.pcan_controller_state", lambda _bus: state)
        assert codec.get_tx_state()["bus"]["status"] == "error"

    def test_a_failed_send_is_an_error_until_it_ages_out(self, codec, monkeypatch):
        codec.bus.send.side_effect = can.CanError("The transmit queue is full")
        with pytest.raises(RuntimeError):
            codec.send_raw(can_id="0x100", data="01")
        bus = codec.get_tx_state()["bus"]
        assert bus["status"] == "error"
        assert bus["health"]["detail"] == "Frames could not be sent: The transmit queue is full."

        monkeypatch.setattr("zelos_extension_can.health.RECENT_S", 0.0)
        assert codec.get_tx_state()["bus"]["status"] == "active"

    def test_an_unavailable_adapter_is_an_error(self, codec, monkeypatch):
        monkeypatch.setattr(
            "zelos_extension_can.codec.pcan_controller_state", lambda _bus: "unavailable"
        )
        bus = codec.get_tx_state()["bus"]
        assert bus["status"] == "error"
        assert "Check its USB connection" in bus["health"]["detail"]

    def test_a_bus_being_reconnected_is_an_error_not_stopped(self, codec):
        mocked, codec.bus = codec.bus, None
        try:
            bus = codec.get_tx_state()["bus"]
        finally:
            codec.bus = mocked
        assert bus["status"] == "error"
        assert bus["health"] == {
            "state": "unavailable",
            "detail": "The bus is down; reconnecting to the adapter.",
        }

    def test_the_reason_names_what_happened_then_why(self, codec, monkeypatch):
        monkeypatch.setattr(
            "zelos_extension_can.codec.pcan_controller_state", lambda _bus: "warning"
        )
        codec.bus.send.side_effect = can.CanError("The transmit queue is full")
        with pytest.raises(RuntimeError):
            codec.send_raw(can_id="0x100", data="01")
        detail = codec.get_tx_state()["bus"]["health"]["detail"]
        assert detail.startswith("Frames could not be sent: The transmit queue is full. ")
        assert detail.endswith(
            "Check the wiring, termination, and that another node runs at this bitrate."
        )

    def test_a_gone_interface_is_an_error_not_active(self, codec, monkeypatch):
        _Ip(monkeypatch, None)
        _as_socketcan(codec)
        bus = codec.get_tx_state()["bus"]
        assert bus["status"] == "error"
        assert bus["health"]["detail"] == "can0 is gone. Check its USB connection."

    def test_a_down_interface_names_the_command_that_brings_it_back(self, codec, monkeypatch):
        _Ip(monkeypatch, _link_json("STOPPED", flags=_DOWN))
        _as_socketcan(codec)
        bus = codec.get_tx_state()["bus"]
        assert bus["status"] == "error"
        assert bus["health"]["detail"] == (
            "can0 is down. Bring it up: sudo ip link set can0 up type can bitrate 250000"
        )

    def test_a_recent_error_frame_never_overrides_the_kernels_live_state(self, codec, monkeypatch):
        import time as _time

        from zelos_extension_can.health import HEALTH_DETAIL

        _Ip(monkeypatch, _link_json("ERROR-ACTIVE"))
        _as_socketcan(codec, "socketcan-py")
        codec.health._error_frame = (_time.monotonic(), "bus_off", HEALTH_DETAIL["bus_off"])
        bus = codec.get_tx_state()["bus"]
        assert (bus["status"], bus["health"]["state"]) == ("active", "ok")

    def test_an_error_frame_refines_the_reason_for_the_same_state(self, codec, monkeypatch):
        _Ip(monkeypatch, _link_json("ERROR-PASSIVE"))
        _as_socketcan(codec, "socketcan-py")
        no_ack = can.Message(arbitration_id=0x24, is_error_frame=True, data=bytes([0, 0x20]))
        codec.on_message_received(no_ack)
        health = codec.get_tx_state()["bus"]["health"]
        assert health["state"] == "passive"
        assert health["detail"].startswith("No node acknowledged")

    def test_pcan_status_outranks_a_recent_error_frame(self, codec, monkeypatch):
        monkeypatch.setattr("zelos_extension_can.codec.pcan_controller_state", lambda _bus: "ok")
        codec.on_message_received(can.Message(arbitration_id=0, is_error_frame=True))
        bus = codec.get_tx_state()["bus"]
        assert (bus["status"], bus["health"]["state"]) == ("active", "ok")
        assert codec.metrics.error_frames == 1

    def test_a_stopped_bus_is_stopped_whatever_it_last_saw(self, codec):
        codec.bus.send.side_effect = can.CanError("down")
        with pytest.raises(RuntimeError):
            codec.send_raw(can_id="0x100", data="01")
        codec.stop()
        bus = codec.get_tx_state()["bus"]
        assert bus["status"] == "stopped"
        assert bus["health"]["state"] == "unknown"


class TestErrorFrames:
    def test_an_error_frame_is_counted_not_traced_or_decoded(self, codec):
        frame = can.Message(arbitration_id=0x20, is_error_frame=True, data=bytes(8))
        with patch.object(codec.decoder, "handle") as decode:
            codec.on_message_received(frame)
        decode.assert_not_called()
        metrics = codec.get_tx_state()["bus"]["metrics"]
        assert metrics["error_frames"] == 1
        assert metrics["messages_received"] == 0

    def test_error_frames_mark_the_bus(self, codec):
        codec.on_message_received(can.Message(arbitration_id=0, is_error_frame=True))
        bus = codec.get_tx_state()["bus"]
        assert bus["status"] == "warning"
        assert bus["health"]["state"] == "warning"
        assert "saw errors on the wire" in bus["health"]["detail"]

    def test_lost_arbitration_is_counted_but_marks_nothing(self, codec):
        _as_socketcan(codec, "socketcan-py")
        codec.on_message_received(
            can.Message(arbitration_id=0x02, is_error_frame=True, data=bytes(8))
        )
        assert codec.metrics.error_frames == 1
        assert codec.health._error_frame is None


class _FlakyBus(VirtualBus):
    """A virtual bus whose first sends fail the way a full PCAN queue does."""

    def __init__(self, failures: int):
        super().__init__(channel="flaky-periodic")
        self.failures = failures
        self.sent = 0

    def send(self, msg, timeout=None):
        if self.failures > 0:
            self.failures -= 1
            raise can.CanOperationError("Failed to send: The transmit queue is full")
        self.sent += 1
        super().send(msg, timeout)


class TestPeriodicSurvivesFailedSends:
    def test_a_periodic_keeps_sending_after_the_bus_recovers(self, codec):
        import time as _time

        mocked = codec.bus
        flaky = _FlakyBus(failures=5)
        codec.bus = flaky
        try:
            codec.start_periodic_raw(can_id="0x200", data="0102", period_ms=5)
            deadline = _time.monotonic() + 5
            while flaky.sent < 10 and _time.monotonic() < deadline:
                _time.sleep(0.01)
            assert flaky.sent >= 10, "the periodic died on its first failed send"
            assert codec.get_tx_state()["bus"]["metrics"]["tx_errors"] == 5
        finally:
            for tid in list(codec.periodics.tasks):
                codec.periodics.stop(tid)
            codec.bus = mocked
            flaky.shutdown()

    def test_a_bus_that_starts_its_own_thread_task_still_gets_the_handler(self, codec):
        class _OwnPeriodic(VirtualBus):
            def _send_periodic_internal(self, *args, **kwargs):
                return super()._send_periodic_internal(*args, **kwargs)

        mocked = codec.bus
        bus = _OwnPeriodic(channel="own-periodic")
        codec.bus = bus
        try:
            codec.start_periodic_raw(can_id="0x201", data="01", period_ms=50)
            (task,) = codec.periodics.tasks.values()
            assert task.on_error(can.CanOperationError("The transmit queue is full")) is True
        finally:
            for tid in list(codec.periodics.tasks):
                codec.periodics.stop(tid)
            codec.bus = mocked
            bus.shutdown()


class TestFramesTheBusCanCarry:
    """A frame a driver cannot carry is refused when asked for, not failed on
    every period of a periodic."""

    def test_more_than_eight_bytes_on_classic_can_is_refused(self, codec):
        with pytest.raises(ValueError, match="should be <= 8"):
            codec.start_periodic_raw(can_id="0x100", data="00" * 12, period_ms=10)
        assert codec.periodics.tasks == {}
        assert codec.periodics.slots == {}
        codec.bus.send.assert_not_called()

    def test_can_fd_on_a_classic_bus_is_refused(self, codec):
        with pytest.raises(ValueError, match="turn on CAN-FD Mode"):
            codec.send_raw(can_id="0x100", data="00" * 12, is_fd=True)
        codec.bus.send.assert_not_called()

    def test_an_fd_bus_takes_an_fd_frame(self, codec):
        codec.fd_mode = True
        codec.send_raw(can_id="0x100", data="00" * 12, is_fd=True)
        (sent,) = codec.bus.send.call_args.args
        assert (sent.is_fd, len(sent.data)) == (True, 12)

    def test_a_dbc_message_goes_out_as_the_dbc_defines_it(self, codec, monkeypatch):
        dbc_msg = codec.catalog.resolve("DUT_Command")
        signals = json.dumps({s.name: 0 for s in dbc_msg.signals})
        monkeypatch.setattr(dbc_msg, "is_fd", True)
        codec.fd_mode = True
        codec.send_message("DUT_Command", signals)
        assert codec.bus.send.call_args.args[0].is_fd is True

    def test_a_can_fd_dbc_message_that_fits_goes_out_classic_on_a_classic_bus(
        self, codec, monkeypatch
    ):
        # A DBC that marks its messages CAN FD still drives a bus without
        # CAN-FD Mode, as long as each message fits a classic frame.
        dbc_msg = codec.catalog.resolve("DUT_Command")
        signals = json.dumps({s.name: 0 for s in dbc_msg.signals})
        monkeypatch.setattr(dbc_msg, "is_fd", True)
        codec.send_message("DUT_Command", signals)
        assert codec.bus.send.call_args.args[0].is_fd is False
        tid = codec.start_periodic_message("DUT_Command", signals, period_ms=10)["task_id"]
        assert codec.periodics.specs[tid][0].is_fd is False
        assert codec.get_tx_state()["bus"]["periodics"][0]["is_fd"] is False

    def test_a_can_fd_dbc_message_over_eight_bytes_needs_can_fd_mode(self, codec, monkeypatch):
        dbc_msg = codec.catalog.resolve("CANFD_BulkData")
        signals = json.dumps({s.name: 0 for s in dbc_msg.signals})
        monkeypatch.setattr(dbc_msg, "is_fd", True)
        with pytest.raises(ValueError, match="turn on CAN-FD Mode"):
            codec.send_message("CANFD_BulkData", signals)
        with pytest.raises(ValueError, match="turn on CAN-FD Mode"):
            codec.start_periodic_message("CANFD_BulkData", signals, period_ms=10)
        assert codec.periodics.tasks == {}
        codec.bus.send.assert_not_called()


class TestPeriodicSlots:
    def test_a_periodic_that_fails_for_another_reason_ends_marked_inactive(self, codec):
        import time as _time

        class _Rejecting(VirtualBus):
            def send(self, msg, timeout=None):
                raise ValueError("invalid index")

        mocked = codec.bus
        bus = _Rejecting(channel="rejecting")
        codec.bus = bus
        try:
            codec.start_periodic_raw(can_id="0x202", data="01", period_ms=5)
            (task,) = codec.periodics.tasks.values()
            deadline = _time.monotonic() + 3
            while not task.stopped and _time.monotonic() < deadline:
                _time.sleep(0.01)
            assert task.stopped
            (slot,) = codec.get_tx_state()["bus"]["periodics"]
            assert slot["is_active"] is False
            assert codec.metrics.tx_errors == 1
        finally:
            for tid in list(codec.periodics.tasks):
                codec.periodics.stop(tid)
            codec.bus = mocked
            bus.shutdown()

    def test_one_slot_holds_one_task(self, codec):
        from unittest.mock import MagicMock

        codec.bus.send_periodic.side_effect = lambda *_a, **_k: MagicMock()
        msg = can.Message(arbitration_id=0x10, data=b"\x01")
        codec.periodics.start(codec.bus, "t", msg, 0.1, "raw")
        first = codec.periodics.tasks["t"]
        codec.periodics.start(codec.bus, "t", msg, 0.1, "raw")
        first.stop.assert_called_once()
        assert codec.periodics.tasks["t"] is not first

    def test_a_periodic_asked_for_while_reconnecting_says_so(self, codec):
        mocked, codec.bus = codec.bus, None
        try:
            with pytest.raises(RuntimeError, match="reconnecting"):
                codec.start_periodic_raw(can_id="0x10", data="01", period_ms=100)
        finally:
            codec.bus = mocked


class TestPcanCanFd:
    """PCAN opens CAN FD only from explicit bit timing; the extension derives it."""

    @staticmethod
    def _bus_kwargs(**config) -> dict:
        cfg = {"interface": "pcan", "channel": "PCAN_USBBUS1", "fd_mode": True, **config}
        with patch("zelos_sdk.TraceSource"), patch("can.Bus") as bus:
            codec = CanCodec(cfg, bus_name="fd")
            try:
                codec.start()
                return bus.call_args.kwargs
            finally:
                codec.stop()

    def test_fd_gets_timing_from_both_bitrates(self):
        kwargs = self._bus_kwargs(bitrate=500_000, data_bitrate=2_000_000)
        timing = kwargs["timing"]
        assert isinstance(timing, can.BitTimingFd)
        assert (timing.f_clock, timing.nom_bitrate, timing.data_bitrate) == (
            80_000_000,
            500_000,
            2_000_000,
        )
        assert "bitrate" not in kwargs
        assert "data_bitrate" not in kwargs

    @pytest.mark.parametrize("nominal", [125_000, 250_000, 500_000, 1_000_000])
    @pytest.mark.parametrize("data", [1_000_000, 2_000_000, 4_000_000, 8_000_000])
    def test_every_bitrate_the_form_offers_has_a_timing(self, nominal, data):
        from zelos_extension_can.pcan import pcan_fd_timing

        timing = pcan_fd_timing(nominal, data)
        assert (timing.nom_bitrate, timing.data_bitrate) == (nominal, data)

    def test_a_data_bitrate_below_the_nominal_one_says_what_to_change(self):
        from zelos_extension_can.pcan import pcan_fd_timing

        with pytest.raises(ValueError, match="Data Bitrate \\(500000\\) is below Bitrate"):
            pcan_fd_timing(1_000_000, 500_000)

    def test_timing_set_under_advanced_wins(self):
        advanced = json.dumps({"f_clock_mhz": 40, "nom_brp": 5})
        kwargs = self._bus_kwargs(bitrate=500_000, data_bitrate=2_000_000, config_json=advanced)
        assert "timing" not in kwargs
        assert kwargs["f_clock_mhz"] == 40

    def test_classic_pcan_keeps_its_bitrate(self):
        kwargs = self._bus_kwargs(bitrate=250_000, fd_mode=False)
        assert kwargs["bitrate"] == 250_000
        assert "timing" not in kwargs

    def test_other_fd_adapters_keep_their_bitrates(self):
        cfg = {
            "interface": "kvaser",
            "channel": "0",
            "fd_mode": True,
            "bitrate": 500_000,
            "data_bitrate": 2_000_000,
        }
        with patch("zelos_sdk.TraceSource"), patch("can.Bus") as bus:
            codec = CanCodec(cfg, bus_name="kv")
            codec.start()
            codec.stop()
        assert bus.call_args.kwargs["data_bitrate"] == 2_000_000
        assert "timing" not in bus.call_args.kwargs


class TestReader:
    """A failed read loses frames, not the reader; only a dead adapter ends it."""

    def test_a_failed_read_is_reported_and_the_next_frame_delivered(self):
        from zelos_extension_can.codec import _Reader

        frames, errors = [], []
        reader = _Reader(frames.append, errors.append)
        overrun = can.CanOperationError("The receive queue was read too late")
        reader.on_error(overrun)
        frame = can.Message(arbitration_id=0x10)
        reader(frame)
        assert (errors, frames) == ([overrun], [frame])

    def test_failures_back_to_back_end_the_reader(self):
        from zelos_extension_can.codec import _Reader

        reader = _Reader(lambda _m: None, lambda _e: None)
        gone = can.CanOperationError("The value of a handle is invalid")
        for _ in range(_Reader.GIVE_UP_AFTER - 1):
            reader.on_error(gone)
        with pytest.raises(can.CanOperationError):
            reader.on_error(gone)

    def test_a_frame_between_failures_starts_the_count_over(self):
        from zelos_extension_can.codec import _Reader

        reader = _Reader(lambda _m: None, lambda _e: None)
        overrun = can.CanOperationError("The receive queue was read too late")
        for _ in range(3):
            for _ in range(_Reader.GIVE_UP_AFTER - 1):
                reader.on_error(overrun)
            reader(can.Message(arbitration_id=0x10))

    def test_failures_far_apart_never_end_the_reader(self, monkeypatch):
        from zelos_extension_can.codec import _Reader

        clock = [0.0]
        monkeypatch.setattr("zelos_extension_can.codec.time.monotonic", lambda: clock[0])
        reader = _Reader(lambda _m: None, lambda _e: None)
        for _ in range(_Reader.GIVE_UP_AFTER * 2):
            clock[0] += _Reader.BACK_TO_BACK_S * 2
            reader.on_error(can.CanOperationError("The receive queue was read too late"))

    def test_an_unexpected_error_is_not_swallowed(self):
        from zelos_extension_can.codec import _Reader

        reader = _Reader(lambda _m: None, lambda _e: None)
        with pytest.raises(KeyError):
            reader.on_error(KeyError("a bug"))

    def test_a_lost_frame_marks_the_bus_and_is_counted(self, codec):
        codec._on_receive_error(can.CanOperationError("The receive queue was read too late"))
        bus = codec.get_tx_state()["bus"]
        assert bus["status"] == "warning"
        assert bus["health"]["detail"] == (
            "Frames were lost on receive: The receive queue was read too late."
        )
        assert bus["metrics"]["rx_errors"] == 1

    def test_the_notifier_keeps_running_through_a_failed_read(self, codec):
        import time as _time

        class _Overrunning(VirtualBus):
            def __init__(self):
                super().__init__(channel="overrun-once")
                self.failed = False

            def _recv_internal(self, timeout):
                if not self.failed:
                    self.failed = True
                    raise can.CanOperationError("The receive queue was read too late")
                return super()._recv_internal(timeout)

        mocked = codec.bus
        bus = _Overrunning()
        peer = VirtualBus(channel="overrun-once")
        codec.bus = bus
        received = []
        codec.decoder.handle = received.append
        try:
            codec._start_notifier()
            assert can.Notifier.find_instances(bus) == (codec._notifier,)
            deadline = _time.monotonic() + 3
            while not bus.failed and _time.monotonic() < deadline:
                _time.sleep(0.01)
            peer.send(can.Message(arbitration_id=0x42, data=b"\x01"))
            while not received and _time.monotonic() < deadline:
                _time.sleep(0.01)
            assert [m.arbitration_id for m in received] == [0x42]
            assert codec._check_notifier_health(codec._notifier)
            assert codec.metrics.rx_errors == 1
        finally:
            codec._stop_notifier()
            codec.bus = mocked
            bus.shutdown()
            peer.shutdown()

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_an_adapter_that_keeps_failing_ends_the_notifier(self, codec):
        """The reconnect takes over from a reader that gave up."""
        import time as _time

        class _Unplugged(VirtualBus):
            def _recv_internal(self, timeout):
                raise can.CanOperationError("The value of a handle is invalid")

        mocked = codec.bus
        bus = _Unplugged(channel="unplugged")
        codec.bus = bus
        try:
            codec._start_notifier()
            deadline = _time.monotonic() + 5
            while codec._check_notifier_health(codec._notifier) and _time.monotonic() < deadline:
                _time.sleep(0.01)
            assert not codec._check_notifier_health(codec._notifier)
        finally:
            codec._stop_notifier()
            codec.bus = mocked
            bus.shutdown()


class TestBusOffRecovery:
    """A PCAN controller left bus-off never sends again; the health check restarts it."""

    @pytest.mark.parametrize(
        ("state", "healthy"), [("bus_off", False), ("warning", True), ("ok", True)]
    )
    def test_only_bus_off_restarts_the_bus(self, codec, monkeypatch, state, healthy):
        codec.config["interface"] = "pcan"
        codec.bus.state = can.BusState.ACTIVE
        monkeypatch.setattr("zelos_extension_can.codec.pcan_controller_state", lambda _bus: state)
        assert codec._check_bus_health() is healthy


class TestRepeatFilter:
    @staticmethod
    def _record(text: str):
        import logging

        return logging.LogRecord("can.bcm", logging.ERROR, __file__, 1, text, None, None)

    def test_a_message_passes_once_per_window(self, monkeypatch):
        from zelos_extension_can.periodics import RepeatFilter

        clock = [0.0]
        monkeypatch.setattr("zelos_extension_can.codec.time.monotonic", lambda: clock[0])
        repeats = RepeatFilter(60.0)
        full = self._record("Failed to send: The transmit queue is full")
        assert repeats.filter(full)
        clock[0] = 30.0
        assert not repeats.filter(full)
        assert repeats.filter(self._record("Failed to send: another reason"))
        clock[0] = 61.0
        assert repeats.filter(full)

    def test_it_forgets_messages_past_the_window(self, monkeypatch):
        from zelos_extension_can.periodics import RepeatFilter

        clock = [0.0]
        monkeypatch.setattr("zelos_extension_can.codec.time.monotonic", lambda: clock[0])
        repeats = RepeatFilter(60.0)
        for n in range(100):
            repeats.filter(self._record(f"reason {n}"))
        clock[0] = 61.0
        repeats.filter(self._record("a new reason"))
        assert len(repeats._last) == 1
