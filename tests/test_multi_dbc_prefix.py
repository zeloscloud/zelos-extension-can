"""Several DBCs per bus, the shared `prefix` source, and the Advanced section.

The parity test is the real gate: the extension's cantools merge and the Rust
`zelos_can` merge must agree on which definition of a CAN id survives.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import can
import cantools
import pytest
import zelos_can

from zelos_extension_can.cli import app as app_mod
from zelos_extension_can.cli.app import (
    ADVANCED_DEFAULTS,
    _create_codecs,
    _prepare_bus_config,
    _resolve_prefix,
    resolve_advanced,
)
from zelos_extension_can.codec import CanCodec, merge_dbc_messages
from zelos_extension_can.converter import convert_can_trace

FILES = Path(__file__).parent / "files"
DBC_A = FILES / "merge_a.dbc"
DBC_B = FILES / "merge_b.dbc"
DBC_C = FILES / "merge_c.dbc"
MERGE_SET = [DBC_A, DBC_B, DBC_C]
TEST_DBC = FILES / "test.dbc"


def _load(paths):
    return [cantools.database.load_file(str(p)) for p in paths]


# ── merge: dedupe, conflict, policy ──────────────────────────────────────────


def test_merge_dedupes_identical_and_lets_the_later_file_win(caplog):
    with caplog.at_level(logging.WARNING, logger="zelos_extension_can.codec"):
        messages, origin, deduped, conflicts = merge_dbc_messages(MERGE_SET, _load(MERGE_SET))

    assert [m.name for m in messages] == [
        "Merge_A",
        "Merge_Same",
        "Merge_Conflict_B",
        "Merge_B",
        "Merge_C",
    ]
    assert deduped == 1  # Merge_Same, defined the same way in A and B
    assert [c["frame_id"] for c in conflicts] == [770]
    assert conflicts[0] == {
        "frame_id": 770,
        "is_extended": False,
        "kept": {"file": "merge_b.dbc", "name": "Merge_Conflict_B"},
        "dropped": {"file": "merge_a.dbc", "name": "Merge_Conflict_A"},
    }
    # Origin tracks which file each surviving definition came from.
    assert origin[(768, False)] == DBC_A
    assert origin[(770, False)] == DBC_B

    warning = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    for fragment in ("0x302", "merge_a.dbc", "merge_b.dbc", "Merge_Conflict_A", "Merge_Conflict_B"):
        assert fragment in warning
    assert "keeping 'Merge_Conflict_B' from merge_b.dbc" in warning


def test_merge_error_policy_refuses_the_load():
    with pytest.raises(ValueError, match="conflicting definitions of CAN id 0x302"):
        merge_dbc_messages(MERGE_SET, _load(MERGE_SET), conflict="error")


def test_codec_error_policy_refuses_the_load():
    config = {
        "interface": "virtual",
        "channel": "vcan0",
        "database_files": [str(p) for p in MERGE_SET],
        "dbc_conflict": "error",
    }
    with pytest.raises(ValueError, match="conflicting definitions"), patch("zelos_sdk.TraceSource"):
        CanCodec(config, bus_name="busA")


# ── parity: the extension's merge vs the Rust merge ──────────────────────────


def test_merge_matches_the_rust_decoder():
    """Same (frame_id, is_extended) -> event name set, for the same file order.

    `zelos_can.CanDecoder.message_keys()` exposes exactly
    `(frame_id, is_extended, event_name)` for the surviving definitions, so the
    comparison is over that triple: it pins both WHICH ids survive and WHICH
    definition won (the event name carries the message name). Signal-level
    parity is not exposed by the Rust side and is not compared here.
    """
    config = {
        "interface": "virtual",
        "channel": "vcan0",
        "database_files": [str(p) for p in MERGE_SET],
    }
    with patch("zelos_sdk.TraceSource"):
        codec = CanCodec(config, bus_name="parity")

    extension = {
        (msg.frame_id, msg.is_extended_frame, f"{msg.frame_id:04x}_{msg.name}")
        for msg in codec.messages
    }
    decoder = zelos_can.CanDecoder(
        database_file=[str(p) for p in MERGE_SET],
        source_name="parity_rust",
        timestamp_mode="ignore",
    )
    assert extension == set(decoder.message_keys())


def test_merge_matches_the_rust_decoder_on_an_intra_file_conflict():
    """test.dbc defines CAN id 800 twice; both sides must keep the same one."""
    config = {"interface": "virtual", "channel": "vcan0", "database_files": [str(TEST_DBC)]}
    with patch("zelos_sdk.TraceSource"):
        codec = CanCodec(config, bus_name="parity")

    width = lambda msg: 8 if msg.is_extended_frame else 4  # noqa: E731
    extension = {
        (msg.frame_id, msg.is_extended_frame, f"{msg.frame_id:0{width(msg)}x}_{msg.name}")
        for msg in codec.messages
    }
    decoder = zelos_can.CanDecoder(
        database_file=[str(TEST_DBC)], source_name="parity_rust_single", timestamp_mode="ignore"
    )
    assert extension == set(decoder.message_keys())


# ── config normalisation ─────────────────────────────────────────────────────


def test_prepare_bus_config_folds_a_legacy_database_file():
    prepared = _prepare_bus_config(
        {
            "interface": "socketcan",
            "channel": "can0",
            "database_file": str(DBC_A),
            "database_files": [str(DBC_B)],
        },
        TEST_DBC,
        resolve_advanced({}),
    )
    # The legacy single file takes precedence, so it is prepended.
    assert prepared["database_files"] == [str(DBC_A), str(DBC_B)]
    assert "database_file" not in prepared


def test_prepare_bus_config_applies_advanced_defaults():
    prepared = _prepare_bus_config(
        {"interface": "socketcan", "channel": "can0"}, TEST_DBC, resolve_advanced({})
    )
    assert prepared["log_raw_frames"] is True
    assert prepared["receive_own_messages"] is True
    assert prepared["emit_schemas_on_init"] is False
    assert prepared["timestamp_mode"] == "auto"
    assert prepared["database_files"] == []


def test_prepare_bus_config_keeps_a_legacy_per_bus_override():
    advanced = resolve_advanced({"advanced": {"log_raw_frames": True, "timestamp_mode": "auto"}})
    prepared = _prepare_bus_config(
        {
            "interface": "socketcan",
            "channel": "can0",
            "log_raw_frames": False,
            "timestamp_mode": "absolute",
        },
        TEST_DBC,
        advanced,
    )
    assert prepared["log_raw_frames"] is False
    assert prepared["timestamp_mode"] == "absolute"


def test_schema_advanced_section_shape():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parents[1] / "config.schema.json").read_text())
    assert list(schema["properties"]) == ["buses", "advanced"]

    advanced = schema["properties"]["advanced"]
    assert advanced["ui:options"] == {"collapsed": True}
    assert advanced["additionalProperties"] is False
    assert advanced["default"] == {}
    assert {k: v["default"] for k, v in advanced["properties"].items()} == ADVANCED_DEFAULTS

    validator = jsonschema.Draft7Validator(schema)
    # A pre-list, pre-advanced config still validates; a DBC list is optional.
    assert validator.is_valid(
        {
            "log_level": "INFO",
            "buses": [{"interface": "socketcan", "channel": "can0", "database_file": "a.dbc"}],
        }
    )
    assert validator.is_valid({"buses": [{"interface": "socketcan", "channel": "can0"}]})
    assert validator.is_valid({"buses": [{"interface": "demo"}], "advanced": {"prefix": ""}})
    assert not validator.is_valid({"buses": [{"interface": "demo"}], "advanced": {"nope": 1}})

    # Every bus branch that takes databases takes the list, the conflict policy,
    # and keeps the legacy key hidden for old configs.
    branches = schema["properties"]["buses"]["items"]["dependencies"]["interface"]["oneOf"]
    with_dbc = [b for b in branches if "database_files" in b["properties"]]
    assert len(with_dbc) == 7
    for branch in with_dbc:
        props = branch["properties"]
        assert props["database_files"]["type"] == "array"
        assert props["database_files"]["items"]["ui:widget"] == "file-picker"
        assert props["database_files"]["ui:orderable"] is True
        assert props["database_files"]["default"] == []
        assert props["dbc_conflict"]["enum"] == ["warn", "error"]
        assert props["database_file"]["ui:widget"] == "hidden"
        assert "database_files" not in branch.get("required", [])
        assert "database_file" not in branch.get("required", [])
        # The globals moved out of every bus branch.
        assert not ({"log_raw_frames", "receive_own_messages", "timestamp_mode"} & props.keys())


def test_resolve_advanced_honours_a_legacy_top_level_log_level():
    assert resolve_advanced({"log_level": "DEBUG"})["log_level"] == "DEBUG"
    assert resolve_advanced({})["log_level"] == ADVANCED_DEFAULTS["log_level"]
    # An explicit advanced value wins over the legacy one.
    assert (
        resolve_advanced({"log_level": "DEBUG", "advanced": {"log_level": "ERROR"}})["log_level"]
        == "ERROR"
    )


@pytest.mark.parametrize("prefix", ["CAN", "My Bus-1", "", "   "])
def test_resolve_prefix_accepts_source_names_and_clearing(prefix):
    _resolve_prefix({"prefix": prefix})


@pytest.mark.parametrize("prefix", ["CAN/x", "can.0", "a@b", "x:y"])
def test_resolve_prefix_rejects_path_separators(prefix):
    with pytest.raises(SystemExit):
        _resolve_prefix({"prefix": prefix})


# ── wire contract ────────────────────────────────────────────────────────────


@pytest.fixture
def merged_codec():
    config = {
        "interface": "virtual",
        "channel": "vcan0",
        "database_files": [str(p) for p in MERGE_SET],
    }
    with patch("zelos_sdk.TraceSource"), patch("can.Bus"):
        codec = CanCodec(config, bus_name="busA")
        codec.start()
    yield codec
    codec.stop()


def test_get_tx_state_keeps_the_single_dbc_view_and_adds_the_list(merged_codec):
    bus = merged_codec.get_tx_state()["bus"]
    # `dbc.hash` and `dbc_name` are what zelos-app-can-tx reads; they must survive.
    assert bus["dbc"] == {
        "path": str(DBC_A),
        "name": "merge_a.dbc",
        "hash": merged_codec.dbc_hash,
        "message_count": 5,
    }
    assert [d["name"] for d in bus["dbcs"]] == ["merge_a.dbc", "merge_b.dbc", "merge_c.dbc"]
    assert [d["message_count"] for d in bus["dbcs"]] == [3, 3, 1]
    assert {d["path"] for d in bus["dbcs"]} == {str(p) for p in MERGE_SET}
    assert all(len(d["hash"]) == 16 for d in bus["dbcs"])
    assert bus["dbc_conflicts"] == [
        {
            "frame_id": 770,
            "is_extended": False,
            "kept": {"file": "merge_b.dbc", "name": "Merge_Conflict_B"},
            "dropped": {"file": "merge_a.dbc", "name": "Merge_Conflict_A"},
        }
    ]


def test_list_and_describe_report_the_owning_file(merged_codec):
    listed = merged_codec.list_messages()
    assert listed["dbc_name"] == "merge_a.dbc"
    assert listed["dbcs"] == ["merge_a.dbc", "merge_b.dbc", "merge_c.dbc"]
    by_name = {m["name"]: m for m in listed["messages"]}
    assert by_name["Merge_A"]["database"] == "merge_a.dbc"
    assert by_name["Merge_Conflict_B"]["database"] == "merge_b.dbc"
    assert by_name["Merge_C"]["database"] == "merge_c.dbc"
    assert "Merge_Conflict_A" not in by_name

    described = merged_codec.describe_message("Merge_C")
    assert described["dbcs"] == ["merge_a.dbc", "merge_b.dbc", "merge_c.dbc"]
    assert described["message"]["database"] == "merge_c.dbc"


# ── zero-DBC bus ─────────────────────────────────────────────────────────────


def test_bus_without_a_database_logs_raw_only():
    config = {
        "interface": "virtual",
        "channel": "vcan0",
        "database_files": [],
        "log_raw_frames": True,
    }
    with patch("zelos_sdk.TraceSource"), patch("can.Bus"):
        codec = CanCodec(config, bus_name="raw_only")
        codec.start()
    try:
        assert codec.messages == []
        assert codec.list_messages()["messages"] == []
        assert codec.list_messages()["dbc_name"] is None
        assert codec.get_tx_state()["bus"]["dbc"]["path"] is None
        assert codec.get_tx_state()["bus"]["dbcs"] == []
        with pytest.raises(ValueError, match="unknown DBC message"):
            codec.send_message("Anything", "{}")

        # A frame still counts as received, decodes nothing, and is raw-logged.
        codec._handle_message(can.Message(arbitration_id=0x300, data=b"\x01"))
        assert codec.metrics.messages_received == 1
        assert codec.metrics.messages_decoded == 0
        assert codec.metrics.unknown_messages == 1
        assert codec.raw_event.log_at.call_count == 1
    finally:
        codec.stop()


# ── live naming over a python-can virtual bus ────────────────────────────────


@pytest.mark.parametrize("with_prefix", [True, False])
def test_virtual_bus_names_decoded_and_raw_events(with_prefix):
    """Over a real python-can virtual bus: raw frames land as `<bus>/Frame`
    with `is_rx` set and decoded ones as `<bus>/<id>_<Msg>` — on the shared
    source when a prefix is set, unprefixed on the bus's own source when not."""
    events: dict[str, MagicMock] = {}

    def add_event(name, _schema):
        events[name] = MagicMock()
        return events[name]

    config = {
        "interface": "virtual",
        "channel": "zelos-multi-dbc-test",
        "database_files": [str(TEST_DBC)],
        "log_raw_frames": True,
    }
    if with_prefix:
        source = MagicMock()
        source.add_event.side_effect = add_event
        codec = CanCodec(config, bus_name="can0", source=source)
    else:
        with patch("zelos_sdk.TraceSource") as mock_source:
            mock_source.return_value.add_event.side_effect = add_event
            codec = CanCodec(config, bus_name="can0")

    notifier = sender = None
    try:
        codec.start()
        notifier = can.Notifier(codec.bus, [codec])
        sender = can.Bus(interface="virtual", channel=config["channel"])
        sender.send(can.Message(arbitration_id=0x64, data=bytes(8), is_extended_id=False))
        deadline = time.monotonic() + 2.0
        while codec.metrics.messages_received == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        if notifier is not None:
            notifier.stop()
        if sender is not None:
            sender.shutdown()
        codec.stop()

    assert codec.metrics.messages_received == 1
    assert codec.metrics.messages_decoded == 1

    prefix = "can0/" if with_prefix else ""
    assert f"{prefix}Frame" in events
    assert f"{prefix}0064_DUT_Status" in events

    raw_kwargs = events[f"{prefix}Frame"].log_at.call_args.kwargs
    assert raw_kwargs["arbitration_id"] == 0x64
    assert raw_kwargs["is_rx"] is True  # a frame off the bus, not a TX echo
    assert raw_kwargs["is_extended"] is False
    assert raw_kwargs["is_fd"] is False
    assert raw_kwargs["dlc"] == 8

    assert events[f"{prefix}0064_DUT_Status"].log_at.call_count == 1


def test_create_codecs_shares_one_source_across_buses():
    config = {
        "buses": [
            {"interface": "virtual", "channel": "vcan0", "database_files": [str(TEST_DBC)]},
            {"interface": "virtual", "channel": "vcan1", "database_files": [str(TEST_DBC)]},
        ]
    }
    shared = MagicMock()
    with patch("zelos_sdk.TraceSource") as mock_source:
        pairs = _create_codecs(config, TEST_DBC, resolve_advanced({}), shared)

    mock_source.assert_not_called()
    assert [name for _, name in pairs] == ["vcan0", "vcan1"]
    assert all(codec.source is shared for codec, _ in pairs)
    assert [codec.raw_event_name for codec, _ in pairs] == ["vcan0/Frame", "vcan1/Frame"]


# ── converter naming ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prefix,expected",
    [
        ("CAN", {"CAN/my capture/Frame", "CAN/my capture/0064_DUT_Status"}),
        ("", {"my capture/Frame", "my capture/0064_DUT_Status"}),
    ],
)
def test_convert_names_the_source_and_events_by_the_prefix(
    tmp_path, trace_event_paths, prefix, expected
):
    """A conversion is named off the prefix, with the input file's own
    (sanitized) stem as the event segment; clearing the prefix names the
    source after that stem instead."""
    source_log = tmp_path / "my capture.log"
    source_log.write_text("(1704067200.0) can0 064#0000000000000000\n")
    output = tmp_path / "out.trz"

    stats = convert_can_trace(source_log, [TEST_DBC], output, prefix=prefix)

    assert stats.messages_converted == 1
    assert expected <= trace_event_paths(output)


def test_convert_without_a_database_writes_raw_frames_only(tmp_path, trace_event_paths):
    source_log = tmp_path / "raw.log"
    source_log.write_text("(1704067200.0) can0 064#0000000000000000\n")
    output = tmp_path / "raw.trz"

    convert_can_trace(source_log, [], output)

    assert trace_event_paths(output) == {"CAN/raw/Frame"}


def test_log_handler_target_follows_the_prefix(monkeypatch):
    """Logs ride the shared source when a prefix is set (`<prefix>/log`) and a
    standalone `can_log` source when it is cleared."""
    seen: list = []
    monkeypatch.setattr(
        app_mod,
        "TraceLoggingHandler",
        lambda source, **_: seen.append(source) or logging.NullHandler(),
    )

    shared = MagicMock()
    app_mod.trace_log_handler(shared)
    app_mod.trace_log_handler(None)
    assert seen == [shared, "can_log"]
