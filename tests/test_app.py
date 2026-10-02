"""Extension orchestration (`cli.app`) and `config.schema.json`.

The python-can runtime it drives is `zelos_can` and is tested there.
"""

import asyncio
import contextlib
import json
import logging
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock, patch

import can.exceptions
import pytest
from zelos_can import ssh_socketcan
from zelos_can.codec import CanCodec

from zelos_extension_can.cli import app as app_mod
from zelos_extension_can.cli.app import (
    ADVANCED_DEFAULTS,
    _create_codecs,
    _prepare_bus_config,
    _run_codecs_async,
    _validate_name,
    resolve_advanced,
)

TEST_DBC = Path(__file__).parent / "files" / "test.dbc"
SCHEMA_PATH = Path(__file__).parents[1] / "config.schema.json"


def _load_schema():
    return json.loads(SCHEMA_PATH.read_text())


def _ssh_branch(schema):
    branches = schema["properties"]["buses"]["items"]["dependencies"]["interface"]["oneOf"]
    ssh = [b for b in branches if b["properties"]["interface"]["enum"] == ["ssh-socketcan"]]
    assert len(ssh) == 1, "exactly one ssh-socketcan oneOf branch expected"
    return ssh[0]


# ── schema ───────────────────────────────────────────────────────────────────


def test_schema_interface_branches_match_zelos_can():
    """The per-interface branches are zelos-can's fragment, verbatim."""
    fragment = json.loads(files("zelos_can.bus").joinpath("interfaces.schema.json").read_text())
    branches = _load_schema()["properties"]["buses"]["items"]["dependencies"]["interface"]
    assert branches["oneOf"] == fragment


def test_schema_is_valid_and_carries_the_per_bus_block():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _load_schema()
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
    assert {"name", "database_files"} <= bus_props.keys()
    # The legacy singular is NOT declared: `prepare_bus_config` folds it, and the
    # bus object takes keys it does not declare, so an old config still validates
    # (below). Declared hidden instead, it put a bare "database_file" label under
    # the DBC list in the app's form.
    assert "database_file" not in bus_props
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


def test_schema_enum_includes_ssh_socketcan():
    schema = _load_schema()
    enum = schema["properties"]["buses"]["items"]["properties"]["interface"]["enum"]
    assert "ssh-socketcan" in enum


def test_schema_ssh_branch_structure():
    """Structural check (always runs, no jsonschema dep needed)."""
    branch = _ssh_branch(_load_schema())
    assert branch["required"] == ["interface", "remote_host"]
    props = branch["properties"]
    for field in (
        "remote_host",
        "remote_channel",
        "ssh_user",
        "ssh_port",
        "ssh_key_path",
        "ssh_host_key_policy",
        "ssh_extra_opts",
        "ssh_hw_timestamps",
        "fd_mode",
    ):
        assert field in props, f"ssh branch missing property {field!r}"
    assert props["remote_channel"]["default"] == "can0"
    assert props["ssh_port"]["default"] == 22
    assert props["ssh_key_path"]["ui:widget"] == "file-picker"
    # Default "auto": a reimaged edge reconnects with no manual host-key step.
    assert props["ssh_host_key_policy"]["enum"] == ["auto", "strict"]
    assert props["ssh_host_key_policy"]["default"] == "auto"
    # Hardware timestamps by default; the edge's candump decides whether it can.
    assert props["ssh_hw_timestamps"]["default"] is True
    # The remote kernel loopback always echoes TX; there is no receive_own_messages.
    assert "receive_own_messages" not in props


def test_schema_validates_good_ssh_config_and_rejects_missing_host():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft7Validator(_load_schema())

    good = {
        "buses": [
            {
                "interface": "ssh-socketcan",
                "remote_host": "edge",
                "database_files": [str(TEST_DBC)],
            }
        ]
    }
    assert validator.is_valid(good)

    full = {
        "buses": [
            {
                "interface": "ssh-socketcan",
                "remote_host": "edge",
                "remote_channel": "vcan0",
                "ssh_user": "zelos",
                "ssh_port": 2222,
                "ssh_key_path": "/home/z/id_ed25519",
                "ssh_host_key_policy": "strict",
                "ssh_extra_opts": "-J bastion",
                "database_files": [str(TEST_DBC)],
                "name": "edge-bus",
                "fd_mode": False,
            }
        ]
    }
    assert validator.is_valid(full)

    missing_host = {"buses": [{"interface": "ssh-socketcan", "database_files": [str(TEST_DBC)]}]}
    assert not validator.is_valid(missing_host)


# ── advanced settings and names ──────────────────────────────────────────────


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


def test_prepare_bus_config_missing_host_exits():
    with pytest.raises(SystemExit):
        _prepare_bus_config(
            {"interface": "ssh-socketcan", "database_files": [str(TEST_DBC)]},
            Path("/nonexistent/demo.dbc"),
        )


# ── _create_codecs ───────────────────────────────────────────────────────────


def test_single_bus_no_name_derives_from_channel():
    """An unnamed bus is always named after its channel, no 'can_codec'
    special case for the single-bus setup."""
    config = {
        "buses": [{"interface": "virtual", "channel": "vcan0", "database_files": [str(TEST_DBC)]}]
    }

    with patch("zelos_sdk.TraceSource"):
        codecs = _create_codecs(config, TEST_DBC)

    assert len(codecs) == 1
    codec, action_name = codecs[0]
    assert action_name == "vcan0"
    assert codec.bus_name == "vcan0"


def test_multi_bus_defaults_name_to_channel():
    config = {
        "buses": [
            {"interface": "virtual", "channel": "vcan0", "database_files": [str(TEST_DBC)]},
            {"interface": "virtual", "channel": "vcan1", "database_files": [str(TEST_DBC)]},
        ]
    }

    with patch("zelos_sdk.TraceSource"):
        codecs = _create_codecs(config, TEST_DBC)

    assert len(codecs) == 2
    assert codecs[0][0].bus_name == "vcan0"
    assert codecs[0][1] == "vcan0"
    assert codecs[1][0].bus_name == "vcan1"
    assert codecs[1][1] == "vcan1"


def test_multi_bus_rejects_duplicate_names():
    """Explicit or defaulted duplicate names exit."""
    dbc = [str(TEST_DBC)]
    config_dupes = {
        "buses": [
            {"name": "bus", "interface": "virtual", "channel": "vcan0", "database_files": dbc},
            {"name": "bus", "interface": "virtual", "channel": "vcan1", "database_files": dbc},
        ]
    }
    with patch("zelos_sdk.TraceSource"), pytest.raises(SystemExit):
        _create_codecs(config_dupes, TEST_DBC)

    # Same channel = same default name = collision
    config_same_channel = {
        "buses": [
            {"interface": "virtual", "channel": "vcan0", "database_files": dbc},
            {"interface": "virtual", "channel": "vcan0", "database_files": dbc},
        ]
    }
    with patch("zelos_sdk.TraceSource"), pytest.raises(SystemExit):
        _create_codecs(config_same_channel, TEST_DBC)


@pytest.mark.parametrize("name", ["can.0", "can_log"])  # separator, log-source name
def test_create_codecs_rejects_an_illegal_bus_name(name):
    config = {"buses": [{"name": name, "interface": "virtual", "channel": "vcan0"}]}
    with pytest.raises(SystemExit), patch("zelos_sdk.TraceSource"):
        _create_codecs(config, TEST_DBC, resolve_advanced({}))


def test_create_codecs_names_an_ssh_bus_after_its_remote_channel():
    bus = {"interface": "ssh-socketcan", "remote_host": "host", "ssh_user": "user"}
    config = {"buses": [bus, {**bus, "remote_channel": "vcan.1"}]}
    with patch("zelos_sdk.TraceSource"):
        pairs = _create_codecs(config, TEST_DBC, resolve_advanced({}))
    assert [name for _, name in pairs] == ["can0", "vcan_1"]


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


# ── startup failure ──────────────────────────────────────────────────────────


def test_run_codecs_async_propagates_can_error_and_cleans_up(monkeypatch):
    """A CanError from codec.start() propagates out of _run_codecs_async (so
    run_app_mode's except can catch it), and the try/finally still tears down any
    bus that had already started before the failing one."""

    def boom(bus, channel, **kwargs):
        raise can.exceptions.CanInitializationError("cannot reach edge:22 (ssh: timed out)")

    monkeypatch.setattr(ssh_socketcan, "SshTransport", boom)
    codec = CanCodec(
        {
            "interface": "ssh-socketcan",
            "channel": "zelos@edge:vcan0",
            "database_files": [str(TEST_DBC)],
        },
        bus_name="ssh_app_test",
    )

    try:
        with pytest.raises(can.exceptions.CanError):
            asyncio.run(_run_codecs_async([codec]))
    finally:
        with contextlib.suppress(Exception):
            codec.stop()


def test_run_app_mode_exits_cleanly_on_startup_failure(monkeypatch):
    """End to end: a doomed ssh bus makes run_app_mode exit(1) with a logged
    reason rather than propagating a raw CanInitializationError traceback."""

    def boom(bus, channel, **kwargs):
        raise can.exceptions.CanInitializationError(
            "ssh host key for edge is not trusted (ssh: Host key verification failed.)"
        )

    monkeypatch.setattr(ssh_socketcan, "SshTransport", boom)

    # Stub the SDK ceremony run_app_mode performs so the test drives only the
    # start/run path; capture the real codecs it builds so we can stop them.
    created: list[CanCodec] = []

    def capture_create(config, dbc, advanced=None, source=None):
        pairs = _create_codecs(config, dbc, advanced, source)
        created.extend(c for c, _ in pairs)
        return pairs

    monkeypatch.setattr(
        app_mod,
        "load_config",
        lambda: {
            "log_level": "INFO",
            "buses": [
                {
                    "interface": "ssh-socketcan",
                    "remote_host": "edge",
                    "database_files": [str(TEST_DBC)],
                }
            ],
        },
    )
    monkeypatch.setattr(app_mod, "_create_codecs", capture_create)
    monkeypatch.setattr(app_mod.can_actions, "register_actions", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "setup_shutdown_handler", lambda *a, **k: None)
    monkeypatch.setattr(app_mod.zelos_sdk, "init", lambda *a, **k: None)
    monkeypatch.setattr(app_mod.zelos_sdk, "init_global_source", lambda *a, **k: MagicMock())
    monkeypatch.setattr(app_mod, "TraceLoggingHandler", lambda *a, **k: logging.NullHandler())

    try:
        with pytest.raises(SystemExit) as ei:
            app_mod.run_app_mode(demo=False, file=None, demo_dbc_path=Path("/nonexistent/demo.dbc"))
        assert ei.value.code == 1
    finally:
        for c in created:
            with contextlib.suppress(Exception):
                c.stop()
