"""Regression tests: PR #1065 (NaN→null, no data loss) vs PR #1077 (type coercion).

Key invariant: NaN/Infinity in float-typed CAN signals must pass through the
Python layer unchanged so the Rust SDK can convert them to null at serialization
time, rather than being rejected by type coercion.
"""

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import can
import pytest

from zelos_extension_can.codec import CanCodec


@pytest.fixture
def test_dbc_path():
    return str(Path(__file__).parent / "files" / "test.dbc")


@pytest.fixture
def mock_config(test_dbc_path):
    return {
        "interface": "virtual",
        "channel": "vcan0",
        "bitrate": 500000,
        "database_file": test_dbc_path,
    }


@pytest.fixture
def codec(mock_config):
    with patch("zelos_sdk.TraceSource"):
        return CanCodec(mock_config)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_nan_inf_preserved_without_dropping_signals(codec, bad_value):
    """NaN/Infinity must pass through _convert_signals and not drop sibling signals."""
    dbc_msg = codec.db.get_message_by_name("DUT_Status")
    decoded = {
        "state": 1,
        "safety_pin_state": 0,
        "enable_line_state": 1,
        "duplicate_signal": 0,
        "multibit_signal": 2,
        "signed_signal": -1,
        "float_signal": bad_value,
        "small_float_signal": 0.5,
        "SOC_signal": 50.0,
    }

    signals = codec._convert_signals(dbc_msg, decoded, base_only=True)

    # Bad value must reach SDK (not filtered) so Rust serialization converts to null
    assert "float_signal" in signals
    if math.isnan(bad_value):
        assert math.isnan(signals["float_signal"])
    else:
        assert signals["float_signal"] == bad_value
    # Sibling signals must not be dropped
    assert signals["state"] == 1
    assert signals["small_float_signal"] == 0.5


def test_emit_signals_catches_type_error(codec):
    """TypeError from SDK (pre-#1077 type extraction) must be caught, not propagate."""
    mock_event = MagicMock()
    mock_event.log.side_effect = TypeError("can't convert float to int")

    codec._emit_signals(mock_event, {"signal": float("nan")}, None, "test")

    assert codec.metrics.decode_errors == 1


def test_end_to_end_nan_no_crash_no_error(codec):
    """Full path: CAN message → decode → emit with NaN must not crash or count as error."""
    # Patch the DBC message's decode method (the code calls dbc_msg.decode(), not db.decode_message)
    dbc_msg = codec.messages_by_id[0x64]
    with patch.object(dbc_msg, "decode") as mock_decode:
        mock_decode.return_value = {
            "state": 1,
            "safety_pin_state": 0,
            "enable_line_state": 1,
            "duplicate_signal": 0,
            "multibit_signal": 2,
            "signed_signal": -1,
            "float_signal": float("nan"),
            "small_float_signal": 0.5,
            "SOC_signal": 50.0,
        }

        msg = can.Message(arbitration_id=0x64, data=bytes(8), timestamp=15.5)
        codec._handle_message(msg)

        assert codec.metrics.messages_decoded == 1
        assert codec.metrics.decode_errors == 0
