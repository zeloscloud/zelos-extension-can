"""Tests for the free-floating action surface in ``zelos_extension_can.actions``.

The action functions are thin shims over ``CanCodec`` methods, but the routing
layer they add (``CAN_CODECS`` dict lookup, error on unknown codec, the
discovery action) is its own thing and worth covering directly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from zelos_extension_can import actions
from zelos_extension_can.codec import CanCodec

DBC_PATH = Path(__file__).parent / "files" / "test.dbc"


def _make_codec(bus_name: str, channel: str) -> CanCodec:
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
def two_codecs():
    """Two registered codecs ('busA', 'busB') in actions.CAN_CODECS; both
    cleaned up + the registry drained on teardown."""
    a = _make_codec("busA", "vcan0")
    b = _make_codec("busB", "vcan1")
    actions.CAN_CODECS["busA"] = a
    actions.CAN_CODECS["busB"] = b
    yield a, b
    actions.CAN_CODECS.pop("busA", None)
    actions.CAN_CODECS.pop("busB", None)
    a.stop()
    b.stop()


class TestRegistry:
    def test_list_codecs_reflects_registry(self, two_codecs):
        assert actions.list_codecs() == {"codecs": ["busA", "busB"]}

    def test_list_codecs_returns_sorted_output_regardless_of_insert_order(self):
        # Insert in reverse to prove the sort isn't an artifact of dict order.
        b = _make_codec("busB", "vcan1")
        a = _make_codec("busA", "vcan0")
        actions.CAN_CODECS["busB"] = b
        actions.CAN_CODECS["busA"] = a
        try:
            assert actions.list_codecs() == {"codecs": ["busA", "busB"]}
        finally:
            actions.CAN_CODECS.pop("busA", None)
            actions.CAN_CODECS.pop("busB", None)
            a.stop()
            b.stop()

    def test_list_codecs_empty_when_nothing_registered(self):
        # The fixture isn't applied here — CAN_CODECS should be empty between
        # tests. Defensive: assert it's empty so a leak from another test
        # fails loudly here instead of silently.
        assert actions.CAN_CODECS == {}
        assert actions.list_codecs() == {"codecs": []}

    def test_unknown_codec_raises_with_available_list(self, two_codecs):
        with pytest.raises(ValueError, match="Unknown CAN codec 'nope'"):
            actions.get_tx_state("nope")


class TestDispatch:
    def test_get_tx_state_routes_to_named_codec(self, two_codecs):
        a, b = two_codecs
        assert actions.get_tx_state("busA")["bus"]["name"] == "busA"
        assert actions.get_tx_state("busB")["bus"]["name"] == "busB"

    def test_list_messages_routes_to_named_codec(self, two_codecs):
        # Both codecs use the same DBC, so the count matches; the `bus` field
        # is what proves dispatch landed on the right instance.
        assert actions.list_messages("busA")["bus"] == "busA"
        assert actions.list_messages("busB")["bus"] == "busB"

    def test_describe_message_routes_to_named_codec(self, two_codecs):
        msg_name = actions.list_messages("busA")["messages"][0]["name"]
        desc = actions.describe_message("busA", msg_name)
        assert desc["bus"] == "busA"
        assert desc["message"]["name"] == msg_name

    def test_encode_preview_routes_to_named_codec(self, two_codecs):
        # Use Signalless_Message — no required signals, so the encode round-trips
        # without us having to hand-curate a payload for the DBC under test.
        result = actions.encode_preview("busA", "Signalless_Message", "{}")
        assert "data_hex" in result
        assert "can_id" in result

    def test_send_raw_routes_to_named_codec(self, two_codecs):
        # Mutating action — confirms the dispatch landed on the right CanCodec
        # by inspecting which mocked bus saw the `send()` call.
        a, b = two_codecs
        actions.send_raw("busA", "0x100", "01 02 03 04")
        assert a.bus.send.call_count == 1  # type: ignore[attr-defined]
        assert b.bus.send.call_count == 0  # type: ignore[attr-defined]

    def test_stop_periodic_routes_to_named_codec(self, two_codecs):
        # stop_periodic on an unknown task_id is a no-op success — we only need
        # to confirm it executes through the named codec without raising.
        result = actions.stop_periodic("busB", "nonexistent")
        assert result == {"task_id": "nonexistent", "stopped": False}


class TestConverterDbcResolution:
    def test_requires_database_path_or_codec(self, two_codecs, tmp_path):
        # No database_path, no codec — must error.
        result = actions.convert_trace_file(
            input_path=str(tmp_path / "missing.log"),
            database_path="",
            codec="",
        )
        assert result["status"] == "error"
        assert "database_path" in result["message"] and "codec" in result["message"]

    def test_codec_fallback_uses_codecs_dbc(self, two_codecs, tmp_path):
        # Input doesn't exist — we only care that codec resolution gets past
        # the "neither was given" guard. The "Input file not found" branch
        # proves we successfully resolved a database from the codec.
        result = actions.convert_trace_file(
            input_path=str(tmp_path / "missing.log"),
            database_path="",
            codec="busA",
        )
        assert result["status"] == "error"
        assert "Input file not found" in result["message"]

    def test_unknown_codec_in_fallback_is_explicit_error(self, two_codecs, tmp_path):
        result = actions.convert_trace_file(
            input_path=str(tmp_path / "missing.log"),
            database_path="",
            codec="nope",
        )
        assert result["status"] == "error"
        assert "Unknown CAN codec" in result["message"]
