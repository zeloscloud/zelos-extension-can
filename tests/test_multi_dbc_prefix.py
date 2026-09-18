"""Several DBCs per bus, the shared `prefix` source, and the Advanced section.

The merge rule itself is the Rust decoder's — `CanCodec` asks `zelos_can` which
definition of a CAN id survives — so there is nothing to test for parity.
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
import zelos_sdk
from conftest import trace_event_paths, trace_events, wait_until

from zelos_extension_can.cli.app import (
    ADVANCED_DEFAULTS,
    _create_codecs,
    _prepare_bus_config,
    _validate_name,
    resolve_advanced,
)
from zelos_extension_can.codec import CanCodec, _merge_dbcs
from zelos_extension_can.converter import convert_can_trace

FILES = Path(__file__).parent / "files"
DBC_A = FILES / "merge_a.dbc"
DBC_B = FILES / "merge_b.dbc"
DBC_C = FILES / "merge_c.dbc"
MERGE_SET = [DBC_A, DBC_B, DBC_C]
TEST_DBC = FILES / "test.dbc"


def _merged_config(**extra) -> dict:
    return {
        "interface": "virtual",
        "channel": "vcan0",
        "database_files": [str(p) for p in MERGE_SET],
        **extra,
    }


# ── merge: dedupe, overlap, conflict, policy ─────────────────────────────────


def test_merge_dedupes_overlaps_and_reports_the_conflict(caplog):
    """merge_a/merge_b define 0x301 identically (silent dedupe), 0x302 under two
    DIFFERENT names (an overlap — both survive and both decode) and 0x303 under
    the SAME name laid out two ways (a conflict — the later file wins, with one
    warning)."""
    with (
        caplog.at_level(logging.INFO, logger="zelos_extension_can.codec"),
        patch("zelos_sdk.TraceSource"),
    ):
        codec = CanCodec(_merged_config(), bus_name="busA")

    # File order, not id order: merge_a's 0x352 precedes merge_b's 0x310.
    assert [(m.name, m.frame_id) for m in codec.messages] == [
        ("Merge_A", 768),
        ("Merge_Same", 769),
        ("Merge_Conflict_A", 770),
        ("Merge_Dup", 771),
        ("Merge_Moved", 850),
        ("Merge_B", 784),
        ("Merge_Conflict_B", 770),
        ("Merge_Moved", 820),
        ("Merge_C", 800),
    ]
    # An overlapping id carries every definition, in definition order.
    assert [m.name for m in codec.messages_by_id[(770, False)]] == [
        "Merge_Conflict_A",
        "Merge_Conflict_B",
    ]
    # A name at two ids resolves to the LATER FILE's definition (merge_b, 0x334),
    # not the higher id (merge_a, 0x352).
    assert codec._resolve_dbc_message("Merge_Moved").frame_id == 820
    # Same id AND same name, different layout: the only conflict shape left.
    assert codec.dbc_conflicts == [
        {
            "frame_id": 771,
            "is_extended": False,
            "kept": {"file": "merge_b.dbc", "name": "Merge_Dup"},
            "dropped": {"file": "merge_a.dbc", "name": "Merge_Dup"},
        }
    ]
    assert codec.message_origin[(768, False, "Merge_A")] == DBC_A
    assert codec.message_origin[(771, False, "Merge_Dup")] == DBC_B

    # The conflict warns, naming both files and the winner; the overlap is INFO,
    # naming the id and both definitions.
    warning = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    for fragment in (str(DBC_A), str(DBC_B), "keeping 'Merge_Dup'"):
        assert fragment in warning
    info = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    for fragment in ("0x302", "'Merge_Conflict_A'", "'Merge_Conflict_B'"):
        assert fragment in info


def test_unpaired_merge_keys_are_reported(caplog):
    """The merge pairs cantools definitions to decoder survivors by (id, name),
    so a definition the two parsers NAME differently pairs with nothing and
    silently vanishes from TX and describe — which is what a DBC-attribute
    rename cantools applies and the Rust parser does not produces
    (`SystemMessageLongSymbol` is the one seen in the field). Modelled by
    renaming the cantools message: both halves must be reported, and the load
    must continue."""
    db = cantools.database.load_file(str(DBC_C))
    db.messages[0].name = "Renamed_Long_Symbol"  # cantools' name, not the file's

    with caplog.at_level(logging.ERROR, logger="zelos_extension_can.codec"):
        messages, _origin, _conflicts, _overlaps, counts = _merge_dbcs([DBC_C], [db], "warn")

    errors = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    for fragment in ("Renamed_Long_Symbol", "Merge_C", "merge_c.dbc", "0x320"):
        assert fragment in errors
    # Reported, never fatal: the load returns, minus the unpairable definition.
    assert messages == []
    assert counts == [1]


def test_codec_error_policy_refuses_the_load():
    """`dbc_conflict: error` is the Rust loader's refusal, surfaced verbatim."""
    with (
        pytest.raises(RuntimeError, match="conflicting DBC layouts"),
        patch("zelos_sdk.TraceSource"),
    ):
        CanCodec(_merged_config(dbc_conflict="error"), bus_name="busA")


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


def test_schema_is_valid_and_carries_the_per_bus_block():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parents[1] / "config.schema.json").read_text())
    jsonschema.Draft7Validator.check_schema(schema)

    advanced = schema["properties"]["advanced"]
    assert advanced["additionalProperties"] is False
    assert advanced["default"] == {}
    assert {k: v["default"] for k, v in advanced["properties"].items()} == ADVANCED_DEFAULTS
    # An emptied text field is saved as "" rather than dropped, so the prefix
    # can be cleared from the app.
    assert advanced["properties"]["prefix"]["ui:emptyValue"] == ""

    # The interface-independent per-bus block is hoisted out of the branches, so
    # it is declared exactly once.
    bus_props = schema["properties"]["buses"]["items"]["properties"]
    assert {"name", "database_files", "dbc_conflict", "database_file"} <= bus_props.keys()
    branches = schema["properties"]["buses"]["items"]["dependencies"]["interface"]["oneOf"]
    assert not any(set(b["properties"]) - {"interface"} & bus_props.keys() for b in branches)

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


def test_resolve_advanced_honours_a_legacy_top_level_log_level():
    assert resolve_advanced({"log_level": "DEBUG"})["log_level"] == "DEBUG"
    assert resolve_advanced({})["log_level"] == ADVANCED_DEFAULTS["log_level"]
    # An explicit advanced value wins over the legacy one.
    assert (
        resolve_advanced({"log_level": "DEBUG", "advanced": {"log_level": "ERROR"}})["log_level"]
        == "ERROR"
    )


def test_resolve_advanced_distinguishes_a_cleared_prefix_from_an_absent_one():
    assert resolve_advanced({})["prefix"] == "CAN"
    assert resolve_advanced({"advanced": {"prefix": ""}})["prefix"] == ""


def test_validate_name_rejects_a_catalog_separator():
    with pytest.raises(SystemExit):
        _validate_name("CAN/x", "Prefix")


@pytest.mark.parametrize("name", ["can.0", "can_log"])  # separator, log-source name
def test_create_codecs_rejects_an_illegal_bus_name(name):
    config = {"buses": [{"name": name, "interface": "virtual", "channel": "vcan0"}]}
    with pytest.raises(SystemExit), patch("zelos_sdk.TraceSource"):
        _create_codecs(config, TEST_DBC, resolve_advanced({}))


def test_create_codecs_sanitizes_a_channel_derived_bus_name():
    config = {"buses": [{"interface": "ssh-socketcan", "remote_host": "host", "ssh_user": "user"}]}
    with patch("zelos_sdk.TraceSource"):
        pairs = _create_codecs(config, TEST_DBC, resolve_advanced({}))
    assert [name for _, name in pairs] == ["user_host_can0"]


# ── wire contract ────────────────────────────────────────────────────────────


@pytest.fixture
def merged_codec():
    with patch("zelos_sdk.TraceSource"), patch("can.Bus"):
        codec = CanCodec(_merged_config(), bus_name="busA")
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
        "message_count": 9,
    }
    assert [d["name"] for d in bus["dbcs"]] == ["merge_a.dbc", "merge_b.dbc", "merge_c.dbc"]
    assert [d["message_count"] for d in bus["dbcs"]] == [5, 5, 1]
    assert {d["path"] for d in bus["dbcs"]} == {str(p) for p in MERGE_SET}
    assert all(len(d["hash"]) == 16 for d in bus["dbcs"])
    assert bus["dbc_conflicts"] == merged_codec.dbc_conflicts
    assert bus["dbc_overlaps"] == [
        {
            "frame_id": 770,
            "is_extended": False,
            "names": [
                {"file": "merge_a.dbc", "name": "Merge_Conflict_A"},
                {"file": "merge_b.dbc", "name": "Merge_Conflict_B"},
            ],
        }
    ]


def test_list_and_describe_report_the_owning_file(merged_codec):
    listed = merged_codec.list_messages()
    assert listed["dbc_name"] == "merge_a.dbc"
    assert listed["dbcs"] == ["merge_a.dbc", "merge_b.dbc", "merge_c.dbc"]
    by_name = {m["name"]: m for m in listed["messages"]}
    assert by_name["Merge_A"]["database"] == "merge_a.dbc"
    assert by_name["Merge_C"]["database"] == "merge_c.dbc"
    # Different names on 0x302: two entries, each with its owning file.
    assert by_name["Merge_Conflict_A"]["database"] == "merge_a.dbc"
    assert by_name["Merge_Conflict_B"]["database"] == "merge_b.dbc"
    assert by_name["Merge_Conflict_A"]["can_id"] == by_name["Merge_Conflict_B"]["can_id"] == 770
    # One entry per NAME, so the name at two ids appears once — as the
    # definition a transmit by that name reaches (merge_b, 0x334) — and the one
    # it shadows is named, not silently missing.
    assert [m["name"] for m in listed["messages"]].count("Merge_Moved") == 1
    assert by_name["Merge_Moved"]["can_id"] == 820
    assert by_name["Merge_Moved"]["database"] == "merge_b.dbc"
    assert by_name["Merge_Moved"]["shadowed"] == [
        {"file": "merge_a.dbc", "can_id": 850, "is_extended": False}
    ]
    assert by_name["Merge_A"]["shadowed"] == []

    described = merged_codec.describe_message("Merge_C")
    assert described["dbcs"] == ["merge_a.dbc", "merge_b.dbc", "merge_c.dbc"]
    assert described["message"]["database"] == "merge_c.dbc"
    assert described["message"]["shadowed"] == []
    # describe names the SAME definition as the list entry, with the same list.
    moved = merged_codec.describe_message("Merge_Moved")["message"]
    assert moved["can_id"] == 820
    assert moved["shadowed"] == by_name["Merge_Moved"]["shadowed"]


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

    def add_event(name, _schema, event_type=None):
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


def test_virtual_bus_decodes_an_overlapping_id_under_every_definition(tmp_path):
    """One frame on 0x302, which merge_a and merge_b define under different
    names: a table per definition, each stamped with the family event type."""
    namespace = zelos_sdk.TraceNamespace("overlap_decode")
    output = tmp_path / "overlap.trz"
    config = _merged_config(channel="zelos-overlap-test", log_raw_frames=False)

    notifier = sender = None
    with zelos_sdk.TraceWriter(str(output), namespace=namespace):
        source = zelos_sdk.TraceSource("CAN", namespace=namespace)
        codec = CanCodec(config, namespace=namespace, bus_name="busA", source=source)
        try:
            codec.start()
            notifier = can.Notifier(codec.bus, [codec])
            sender = can.Bus(interface="virtual", channel=config["channel"])
            sender.send(can.Message(arbitration_id=0x302, data=bytes(2), is_extended_id=False))
            wait_until(lambda: codec.metrics.messages_decoded >= 2)
        finally:
            if notifier is not None:
                notifier.stop()
            if sender is not None:
                sender.shutdown()
            codec.stop()

    # One frame, two definitions, two decodes — the same count the Rust codec reports.
    assert codec.metrics.messages_received == 1
    assert codec.metrics.messages_decoded == 2

    written = trace_events(output)
    assert written["CAN/busA/0302_Merge_Conflict_A"] == "zelos.can.message.v1"
    assert written["CAN/busA/0302_Merge_Conflict_B"] == "zelos.can.message.v1"


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
def test_convert_names_the_source_and_events_by_the_prefix(tmp_path, prefix, expected):
    """A conversion is named off the prefix, with the input file's own
    (sanitized) stem as the event segment; clearing the prefix names the
    source after that stem instead."""
    source_log = tmp_path / "my capture.log"
    source_log.write_text("(1704067200.0) can0 064#0000000000000000\n")
    output = tmp_path / "out.trz"

    stats = convert_can_trace(source_log, [TEST_DBC], output, prefix=prefix)

    assert stats.messages_converted == 1
    assert expected <= trace_event_paths(output)


def test_convert_without_a_database_writes_raw_frames_only(tmp_path):
    source_log = tmp_path / "raw.log"
    source_log.write_text("(1704067200.0) can0 064#0000000000000000\n")
    output = tmp_path / "raw.trz"

    convert_can_trace(source_log, [], output)

    assert trace_event_paths(output) == {"CAN/raw/Frame"}
