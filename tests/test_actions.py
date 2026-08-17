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
    # Failures must *raise* — the actions protocol derives its error verdict
    # from a raised exception, not from a payload key.
    def test_requires_database_path_or_codec(self, two_codecs, tmp_path):
        # No database_path, no codec — must error.
        with pytest.raises(ValueError, match=r"`database_path` or `codec`"):
            actions.convert_trace_file(
                input_path=str(tmp_path / "missing.log"),
                database_path="",
                codec="",
            )

    def test_codec_fallback_uses_codecs_dbc(self, two_codecs, tmp_path):
        # Input doesn't exist — we only care that codec resolution gets past
        # the "neither was given" guard. The "Input file not found" branch
        # proves we successfully resolved a database from the codec.
        with pytest.raises(FileNotFoundError, match="Input file not found"):
            actions.convert_trace_file(
                input_path=str(tmp_path / "missing.log"),
                database_path="",
                codec="busA",
            )

    def test_unknown_codec_in_fallback_is_explicit_error(self, two_codecs, tmp_path):
        with pytest.raises(ValueError, match="Unknown CAN codec"):
            actions.convert_trace_file(
                input_path=str(tmp_path / "missing.log"),
                database_path="",
                codec="nope",
            )


class TestStandaloneConvert:
    """The at-rest ``convert`` action.

    Distinct from ``convert_trace_file`` above: this is the one declared
    ``standalone=True``, so it runs in a one-shot interpreter with no extension
    process, no bus, and nothing listening for its logs. Raising is the only
    failure signal a caller gets, because a plain return means "no verdict",
    which the wire maps to DONE.
    """

    def _log(self, tmp_path: Path) -> Path:
        src = tmp_path / "capture.log"
        src.write_text("(0.0) can0 100#0011223344556677\n")
        return src

    def test_missing_input_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Input file not found"):
            actions.convert(input_file=str(tmp_path / "missing.log"))

    def test_unsupported_format_raises(self, tmp_path):
        src = tmp_path / "capture.wat"
        src.write_text("nope")
        with pytest.raises(ValueError, match="Unsupported format"):
            actions.convert(input_file=str(src))

    def test_no_database_anywhere_raises(self, tmp_path):
        src = self._log(tmp_path)
        with (
            patch.object(actions, "_configured_database_file", return_value=None),
            pytest.raises(ValueError, match="No database_file given"),
        ):
            actions.convert(input_file=str(src))

    def test_missing_database_raises(self, tmp_path):
        src = self._log(tmp_path)
        with pytest.raises(FileNotFoundError, match="Database file not found"):
            actions.convert(input_file=str(src), database_file=str(tmp_path / "nope.dbc"))

    def test_existing_output_without_force_raises(self, tmp_path):
        src = self._log(tmp_path)
        dest = tmp_path / "capture.trz"
        dest.write_text("occupied")
        with pytest.raises(FileExistsError, match="Output exists"):
            actions.convert(input_file=str(src), database_file=str(DBC_PATH), output_file=str(dest))


class TestOpenInApp:
    """`_open_in_app`'s spawn contract.

    Both kwargs asserted here are load-bearing, not cosmetic. A standalone action
    runs in a setsid one-shot whose *process group* the supervisor kills on
    abnormal exit, and whose stdout/stderr it drains to EOF before considering
    the run finished. A child in that group dies with it; a child inheriting
    those pipes holds them open past the action's return, stalling the run and
    then tripping the terminate path.
    """

    def test_spawns_detached_with_no_inherited_pipes(self, tmp_path):
        import subprocess

        target = tmp_path / "out.trz"
        with patch("subprocess.Popen") as popen:
            actions._open_in_app(target)

        assert popen.call_count == 1
        args, kwargs = popen.call_args
        assert kwargs["start_new_session"] is True, "opener must escape the killed process group"
        for stream in ("stdin", "stdout", "stderr"):
            assert kwargs[stream] is subprocess.DEVNULL, f"{stream} must not hold the run's pipes"
        # argv list, never a shell string: the path is caller-supplied.
        assert isinstance(args[0], list)
        assert str(target) in args[0]

    def test_open_failure_does_not_fail_a_finished_conversion(self, tmp_path):
        src = tmp_path / "capture.log"
        src.write_text("(0.0) can0 100#0011223344556677\n")
        dest = tmp_path / "capture.trz"

        class _Stats:
            def to_dict(self):
                return {}

        # `convert` imports convert_can_trace inside the function body, so it
        # must be patched where it is defined, not on the actions module.
        with (
            patch("zelos_extension_can.converter.convert_can_trace", return_value=_Stats()),
            patch.object(actions, "_open_in_app", side_effect=OSError("no opener")),
        ):
            result = actions.convert(
                input_file=str(src),
                database_file=str(DBC_PATH),
                output_file=str(dest),
                open_on_complete=True,
            )

        # The trace exists by this point. A failed open reported as a failed
        # conversion would send the caller to re-run work that already finished.
        assert result["status"] == "success"
        assert result["opened"] is False
        assert "no opener" in result["open_error"]
