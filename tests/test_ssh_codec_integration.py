"""Integration tests for the ssh-socketcan codec wiring (no real ssh).

These drive the *codec* seam of the ssh-socketcan interface — ``_start_ssh``,
the health/reconnect supervisor, ``stop``, the channel synthesis in
``_prepare_bus_config``, and the config schema — without ever spawning ssh.

The only thing stubbed is :class:`SshTransport`: a :class:`_StubTransport`
records its constructor args and exposes a mutable ``healthy`` flag plus a
``teardown`` counter. Everything else is real — a real ``zelos_sdk.TraceSource``,
a real ``zelos_can.ExternalBus`` + ``zelos_can.CanCodec`` behind ``self._native``,
and the real ``CodecTxAdapter`` — so the codec's Rust decode/TX/periodics
machinery is exercised exactly as in production. Because ``_start_ssh`` and
``_reconnect_bus`` both do ``from .ssh_socketcan import SshTransport`` at call
time, monkeypatching the attribute on the module swaps the transport for both.
"""

import asyncio
import contextlib
import itertools
import json
from pathlib import Path

import pytest
import zelos_can

from zelos_extension_can import ssh_socketcan
from zelos_extension_can.cli.app import _create_codecs, _prepare_bus_config
from zelos_extension_can.codec import CanCodec

TEST_DBC = str(Path(__file__).parent / "files" / "test.dbc")
SCHEMA_PATH = Path(__file__).parent.parent / "config.schema.json"

_name_counter = itertools.count()


# ── Stub transport (no ssh, no threads, no procs) ────────────────────────────


class _StubTransport:
    """Stand-in for :class:`SshTransport` that records ctor args and is inert."""

    def __init__(
        self,
        bus,
        channel,
        *,
        ssh_port=22,
        ssh_key_path=None,
        ssh_extra_opts=None,
        fd_mode=False,
    ):
        self.bus = bus
        self.channel = channel
        self.ssh_port = ssh_port
        self.ssh_key_path = ssh_key_path
        self.ssh_extra_opts = ssh_extra_opts
        self.fd_mode = fd_mode
        self.healthy = True
        self.teardowns = 0

    def teardown(self):
        self.teardowns += 1


@pytest.fixture
def stub_transports(monkeypatch):
    """Swap ``SshTransport`` for a recording stub; return the created list."""
    created: list[_StubTransport] = []

    def factory(bus, channel, **kwargs):
        t = _StubTransport(bus, channel, **kwargs)
        created.append(t)
        return t

    monkeypatch.setattr(ssh_socketcan, "SshTransport", factory)
    return created


@pytest.fixture
def make_ssh_codec(stub_transports):
    """Factory for started ssh-socketcan codecs; stops them on teardown."""
    codecs: list[CanCodec] = []

    def _make(overrides=None, *, start=True):
        cfg = {
            "interface": "ssh-socketcan",
            "channel": "zelos@edge:vcan0",
            "database_file": TEST_DBC,
        }
        if overrides:
            cfg.update(overrides)
        codec = CanCodec(cfg, bus_name=f"ssh_itest_{next(_name_counter)}")
        codecs.append(codec)
        if start:
            codec.start()
        return codec

    yield _make
    for codec in codecs:
        with contextlib.suppress(Exception):
            codec.stop()


# ── start(): builds Rust codec + adapter, threads ssh kwargs to the transport ─


def test_start_builds_native_codec_and_adapter(make_ssh_codec, stub_transports):
    codec = make_ssh_codec()

    assert codec.running is True
    assert isinstance(codec._native, zelos_can.CanCodec)
    assert isinstance(codec._ebus, zelos_can.ExternalBus)
    assert isinstance(codec.bus, ssh_socketcan.CodecTxAdapter)
    # The adapter drives the same Rust codec instance.
    assert codec.bus._codec is codec._native
    # Exactly one transport built, fed the durable ExternalBus + the channel.
    assert len(stub_transports) == 1
    transport = stub_transports[0]
    assert codec._transport is transport
    assert codec.bus.transport is transport
    assert transport.bus is codec._ebus
    assert transport.channel == "zelos@edge:vcan0"


def test_start_threads_ssh_kwargs_to_transport(make_ssh_codec, stub_transports):
    make_ssh_codec(
        {
            "ssh_port": 2222,
            "ssh_key_path": "/home/z/id_ed25519",
            "ssh_extra_opts": "-o StrictHostKeyChecking=no",
            "fd_mode": True,
        }
    )
    transport = stub_transports[0]
    assert transport.ssh_port == 2222
    assert transport.ssh_key_path == "/home/z/id_ed25519"
    assert transport.ssh_extra_opts == "-o StrictHostKeyChecking=no"
    assert transport.fd_mode is True


def test_ssh_flags_set_in_init():
    codec = CanCodec(
        {"interface": "ssh-socketcan", "channel": "h:can0", "database_file": TEST_DBC},
        bus_name=f"ssh_itest_{next(_name_counter)}",
    )
    assert codec._use_ssh is True
    assert codec._use_native is False
    assert codec._use_rust is True


# ── health supervisor delegates to the transport via the adapter ─────────────


def test_check_bus_health_tracks_transport(make_ssh_codec):
    codec = make_ssh_codec()
    assert codec._check_bus_health() is True

    codec._transport.healthy = False
    assert codec._check_bus_health() is False

    codec._transport.healthy = True
    assert codec._check_bus_health() is True


# ── reconnect: rebuild ONLY the transport; codec/port/periodics survive ──────


def test_reconnect_rebuilds_only_transport(make_ssh_codec, stub_transports):
    codec = make_ssh_codec()
    native_before = codec._native
    ebus_before = codec._ebus
    old_transport = codec._transport

    # Arm a periodic in the Rust codec (slow period so it survives the rebuild
    # without flooding the undrained outlet).
    result = codec.start_periodic_raw("0x100", "01 02", period_ms=1000)
    shim = codec._periodic_tasks[result["task_id"]]
    assert shim.is_active is True

    old_transport.healthy = False  # simulate a dead ssh link
    ok = asyncio.run(codec._reconnect_bus())

    assert ok is True
    # Only the transport was rebuilt.
    assert old_transport.teardowns == 1
    assert len(stub_transports) == 2
    new_transport = stub_transports[1]
    assert codec._transport is new_transport
    assert codec.bus.transport is new_transport
    assert new_transport is not old_transport
    # Codec, ExternalBus, and the armed periodic all survived.
    assert codec._native is native_before
    assert codec._ebus is ebus_before
    assert shim.is_active is True
    # The rebuilt transport carries the same durable ExternalBus + channel.
    assert new_transport.bus is ebus_before
    assert new_transport.channel == "zelos@edge:vcan0"


def test_reconnect_transport_build_failure_preserves_codec_then_recovers(
    make_ssh_codec, stub_transports, monkeypatch
):
    """A failed transport rebuild is NON-destructive: the codec, ExternalBus,
    bus-object identity, and armed periodics are all left untouched (no fall-back
    to the destructive full rebuild), and a later attempt rebuilds cleanly."""
    codec = make_ssh_codec()
    native_before = codec._native
    ebus_before = codec._ebus
    bus_before = codec.bus
    old_transport = codec._transport

    result = codec.start_periodic_raw("0x100", "01 02", period_ms=1000)
    shim = codec._periodic_tasks[result["task_id"]]
    assert shim.is_active is True

    # First attempt: SshTransport construction fails; second onward: succeeds.
    stub_factory = ssh_socketcan.SshTransport  # the fixture's recording stub
    calls = {"n": 0}

    def flaky(bus, channel, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ssh unreachable")
        return stub_factory(bus, channel, **kwargs)

    monkeypatch.setattr(ssh_socketcan, "SshTransport", flaky)

    ok = asyncio.run(codec._reconnect_bus())

    assert ok is False
    # Old transport reaped, but NOTHING else changed — no second codec, no
    # object-identity churn, periodic still armed.
    assert old_transport.teardowns == 1
    assert codec._native is native_before
    assert codec._ebus is ebus_before
    assert codec.bus is bus_before
    assert codec._transport is old_transport  # not replaced on failure
    assert shim.is_active is True
    assert len(stub_transports) == 1  # no new transport was constructed

    # A subsequent tick succeeds → transport-only rebuild, codec still preserved.
    ok2 = asyncio.run(codec._reconnect_bus())

    assert ok2 is True
    assert codec._native is native_before
    assert codec._ebus is ebus_before
    assert codec.bus is bus_before
    assert codec._transport is not old_transport
    assert codec.bus.transport is codec._transport
    assert shim.is_active is True
    assert len(stub_transports) == 2


def test_reconnect_bails_when_not_running(make_ssh_codec, stub_transports):
    """stop() set running=False first → reconnect is a no-op (no resurrection)."""
    codec = make_ssh_codec()
    old_transport = codec._transport
    codec.running = False

    ok = asyncio.run(codec._reconnect_bus())

    assert ok is False
    assert len(stub_transports) == 1  # nothing rebuilt
    assert codec._transport is old_transport
    assert old_transport.teardowns == 0


def test_reconnect_stop_during_teardown_does_not_resurrect(make_ssh_codec, stub_transports):
    """A stop() that races in DURING the blocking teardown is caught by the
    post-teardown re-check — no transport is resurrected on a stopping codec."""
    codec = make_ssh_codec()
    old_transport = codec._transport
    bus_before = codec.bus

    def teardown_then_stop():
        old_transport.teardowns += 1
        codec.running = False  # simulate stop() landing mid-teardown

    old_transport.teardown = teardown_then_stop

    ok = asyncio.run(codec._reconnect_bus())

    assert ok is False
    assert len(stub_transports) == 1  # re-check bailed before building
    assert codec._transport is old_transport
    assert codec.bus is bus_before


# ── stop(): tears down the transport and snapshots native metrics ────────────


def test_stop_tears_down_transport_and_snapshots(make_ssh_codec):
    codec = make_ssh_codec()
    transport = codec._transport

    codec.stop()

    assert transport.teardowns >= 1
    assert codec._native is None
    assert codec.bus is None
    assert codec.running is False
    # Metrics were snapshotted off the Rust codec before it was dropped.
    assert codec._native_metrics is not None
    assert set(codec._native_metrics) == {
        "messages_received",
        "messages_decoded",
        "unknown_messages",
    }


def test_get_tx_state_rx_counts_from_native(make_ssh_codec):
    codec = make_ssh_codec()
    state = codec.get_tx_state()
    metrics = state["bus"]["metrics"]
    # RX counters come from the Rust codec (self._native), not self.metrics.
    assert "messages_received" in metrics
    assert "messages_decoded" in metrics
    assert "unknown_messages" in metrics
    assert state["bus"]["interface"] == "ssh-socketcan"
    assert state["bus"]["status"] == "active"
    # TX counters are present and integer-valued on the rust path.
    assert isinstance(metrics["tx_errors"], int)
    assert isinstance(metrics["tx_overflows"], int)


def test_get_tx_state_merges_rust_tx_counters(make_ssh_codec, monkeypatch):
    """On the rust path a stalled transport surfaces in the Rust codec's tx
    counters; get_tx_state must MERGE those with the Python-side self.metrics."""
    codec = make_ssh_codec()
    codec.metrics.tx_errors = 3
    codec.metrics.tx_overflows = 2
    monkeypatch.setattr(codec, "_native_tx_counts", lambda: {"tx_errors": 5, "tx_overflows": 7})

    metrics = codec.get_tx_state()["bus"]["metrics"]
    assert metrics["tx_errors"] == 8  # 3 (python) + 5 (rust)
    assert metrics["tx_overflows"] == 9  # 2 (python) + 7 (rust)


# ── _prepare_bus_config: synthesize the [user@]host:iface channel ────────────


def test_prepare_bus_config_channel_with_user():
    cfg = _prepare_bus_config(
        {
            "interface": "ssh-socketcan",
            "remote_host": "edge",
            "ssh_user": "zelos",
            "remote_channel": "vcan0",
            "database_file": TEST_DBC,
        },
        Path("/nonexistent/demo.dbc"),
    )
    assert cfg["channel"] == "zelos@edge:vcan0"


def test_prepare_bus_config_channel_without_user_defaults_can0():
    cfg = _prepare_bus_config(
        {
            "interface": "ssh-socketcan",
            "remote_host": "edge",
            "database_file": TEST_DBC,
        },
        Path("/nonexistent/demo.dbc"),
    )
    assert cfg["channel"] == "edge:can0"


def test_prepare_bus_config_missing_host_exits():
    with pytest.raises(SystemExit):
        _prepare_bus_config(
            {"interface": "ssh-socketcan", "database_file": TEST_DBC},
            Path("/nonexistent/demo.dbc"),
        )


# ── _create_codecs: dotted ssh channels sanitized into safe source names ─────


def test_create_codecs_sanitizes_dotted_ssh_source_names():
    """Unnamed multi-bus derives the trace-source name from the channel. ssh
    channels carry '.', '@', ':' — catalog path separators — which must be
    sanitized to '_' so catalog/`latest` lookups don't break, and the derived
    names stay collision-free."""
    config = {
        "buses": [
            {
                "interface": "ssh-socketcan",
                "remote_host": "192.168.1.10",
                "database_file": TEST_DBC,
            },
            {
                "interface": "ssh-socketcan",
                "remote_host": "10.0.0.5",
                "ssh_user": "zelos",
                "remote_channel": "can1",
                "database_file": TEST_DBC,
            },
        ]
    }
    pairs = _create_codecs(config, Path("/nonexistent/demo.dbc"))
    names = [action_name for _, action_name in pairs]

    assert names == ["192_168_1_10_can0", "zelos_10_0_0_5_can1"]
    for name in names:
        assert not (set(".@:") & set(name)), f"unsanitized separator in {name!r}"
    assert len(set(names)) == len(names)  # collision-free
    # The trace source itself carries the sanitized name.
    assert pairs[0][0].bus_name == "192_168_1_10_can0"


# ── config.schema.json: enum + oneOf branch ─────────────────────────────────


def _load_schema():
    return json.loads(SCHEMA_PATH.read_text())


def _ssh_branch(schema):
    branches = schema["properties"]["buses"]["items"]["dependencies"]["interface"]["oneOf"]
    ssh = [b for b in branches if b["properties"]["interface"]["enum"] == ["ssh-socketcan"]]
    assert len(ssh) == 1, "exactly one ssh-socketcan oneOf branch expected"
    return ssh[0]


def test_schema_enum_includes_ssh_socketcan():
    schema = _load_schema()
    enum = schema["properties"]["buses"]["items"]["properties"]["interface"]["enum"]
    assert "ssh-socketcan" in enum


def test_schema_ssh_branch_structure():
    """Structural check (always runs, no jsonschema dep needed)."""
    branch = _ssh_branch(_load_schema())
    assert branch["required"] == ["interface", "remote_host", "database_file"]
    props = branch["properties"]
    for field in (
        "remote_host",
        "remote_channel",
        "ssh_user",
        "ssh_port",
        "ssh_key_path",
        "ssh_extra_opts",
        "database_file",
        "name",
        "fd_mode",
        "timestamp_mode",
        "log_raw_frames",
        "emit_schemas_on_init",
    ):
        assert field in props, f"ssh branch missing property {field!r}"
    assert props["remote_channel"]["default"] == "can0"
    assert props["ssh_port"]["default"] == 22
    assert props["ssh_key_path"]["ui:widget"] == "file-picker"
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
                "database_file": TEST_DBC,
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
                "ssh_extra_opts": "-o StrictHostKeyChecking=no",
                "database_file": TEST_DBC,
                "name": "edge-bus",
                "fd_mode": False,
                "timestamp_mode": "auto",
                "log_raw_frames": True,
                "emit_schemas_on_init": False,
            }
        ]
    }
    assert validator.is_valid(full)

    missing_host = {"buses": [{"interface": "ssh-socketcan", "database_file": TEST_DBC}]}
    assert not validator.is_valid(missing_host)
