"""Hermetic tests for the ssh-socketcan transport (no real ssh, no network).

``subprocess.Popen`` is monkeypatched with :class:`FakePopen`, which uses real
``os.pipe`` fds for stdout/stderr (so the reader thread's ``os.read`` works) and
an in-memory capture for stdin (so the writer thread's output can be asserted).
A real ``zelos_can.ExternalBus`` + ``CanCodec`` sit behind the port, so RX/TX/
periodics exercise the actual Rust machinery. Every proc and thread started is
torn down by fixtures.
"""

import contextlib
import itertools
import os
import subprocess
import threading
import time
from pathlib import Path

import can
import can.exceptions
import pytest
import zelos_can

from zelos_extension_can import ssh_socketcan

TEST_DBC = str(Path(__file__).parent / "files" / "test.dbc")
WIRE_ID_HEX = "064"  # DUT_Status (id 100, 8 bytes, standard) in test.dbc

_source_counter = itertools.count()


def _wait_until(pred, timeout=3.0, interval=0.01):
    """Poll ``pred`` until truthy or timeout; return its final value."""
    deadline = time.monotonic() + timeout
    val = pred()
    while not val and time.monotonic() < deadline:
        time.sleep(interval)
        val = pred()
    return val


# ── Fake subprocess plumbing ─────────────────────────────────────────────────


class _StdinCapture:
    """In-memory, thread-safe stand-in for a Popen stdin pipe."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.closed = False

    def write(self, data):
        with self._lock:
            if self.closed:
                raise BrokenPipeError("stdin closed")
            self._buf += data
        return len(data)

    def flush(self):
        pass

    def close(self):
        with self._lock:
            self.closed = True

    def getvalue(self):
        with self._lock:
            return bytes(self._buf)


class FakePopen:
    """Popen stand-in backed by real pipes for stdout/stderr."""

    def __init__(self, argv, *, stdin=None, stdout=None, stderr=None):
        self.argv = argv
        self.returncode = None
        self._stdout_w = None
        self.stdout = None
        self._stderr_w = None
        self.stderr = None
        if stdout == subprocess.PIPE:
            r, w = os.pipe()
            self._stdout_w = w
            self.stdout = os.fdopen(r, "rb", buffering=0)
        if stderr == subprocess.PIPE:
            r, w = os.pipe()
            self._stderr_w = w
            self.stderr = os.fdopen(r, "rb", buffering=0)
        self.stdin = _StdinCapture() if stdin == subprocess.PIPE else None

    # -- test drivers --
    def feed(self, data: bytes) -> None:
        """Push bytes onto the fake candump stdout."""
        os.write(self._stdout_w, data)

    def feed_stderr(self, data: bytes) -> None:
        os.write(self._stderr_w, data)

    def close_stdout(self) -> None:
        """Simulate remote EOF (candump exited / ssh closed)."""
        if self._stdout_w is not None:
            with contextlib.suppress(OSError):
                os.close(self._stdout_w)
            self._stdout_w = None

    def close_stderr(self) -> None:
        if self._stderr_w is not None:
            with contextlib.suppress(OSError):
                os.close(self._stderr_w)
            self._stderr_w = None

    def die(self, rc: int = 1) -> None:
        """Simulate the proc exiting without touching its pipes."""
        self.returncode = rc

    # -- Popen surface --
    def poll(self):
        return self.returncode

    def terminate(self):
        # Real proc death EOFs BOTH pipes; model that so the reader and the
        # stderr-drain thread both unblock and teardown joins them promptly.
        if self.returncode is None:
            self.returncode = -15
        self.close_stdout()
        self.close_stderr()

    def kill(self):
        if self.returncode is None:
            self.returncode = -9
        self.close_stdout()
        self.close_stderr()

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def _cleanup(self) -> None:
        self.close_stdout()
        self.close_stderr()
        for f in (self.stdout, self.stderr):
            if f is not None:
                with contextlib.suppress(OSError):
                    f.close()


class FakePopenFactory:
    def __init__(self):
        self.procs = []
        self.calls = []
        self.fail_on_call = None  # 1-based index whose construction raises
        # If set, the RX proc (first Popen) is born already-exited with this
        # stderr + returncode and its stdout closed — models a fast ssh startup
        # failure (host key / auth / unreachable / missing can-utils) that the
        # startup probe must classify and fail fast on.
        self.rx_dead_stderr = None
        self.rx_dead_rc = 1
        # If set, the (alive) RX proc is born with this frame already on its
        # stdout, so the reader sets `_rx_started` and the probe connects early.
        self.rx_born_frame = None

    def __call__(self, argv, *, stdin=None, stdout=None, stderr=None, bufsize=-1, **_kw):
        self.calls.append(argv)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise OSError("simulated Popen failure")
        p = FakePopen(argv, stdin=stdin, stdout=stdout, stderr=stderr)
        self.procs.append(p)
        # The first proc is the RX (candump) side that the startup probe watches.
        if len(self.procs) == 1:
            if self.rx_dead_stderr is not None:
                if self.rx_dead_stderr:
                    p.feed_stderr(self.rx_dead_stderr)
                p.die(self.rx_dead_rc)
                p.close_stdout()  # reader hits EOF → never sets _rx_started
            elif self.rx_born_frame is not None:
                p.feed(self.rx_born_frame)  # reader sets _rx_started → connected
        return p

    @property
    def rx(self):
        return self.procs[0]

    @property
    def tx(self):
        return self.procs[1]

    def cleanup(self):
        for p in self.procs:
            p._cleanup()


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_startup_grace(monkeypatch):
    """Shrink the startup probe's idle grace so the file stays fast. The probe
    LOGIC is unchanged (fail-fast when the rx proc is dead, proceed when it is
    alive) — only the idle wait on an alive-but-silent proc is compressed."""
    monkeypatch.setattr(ssh_socketcan, "_STARTUP_GRACE", 0.2)


@pytest.fixture
def fake_ssh(monkeypatch):
    factory = FakePopenFactory()
    monkeypatch.setattr(ssh_socketcan.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(ssh_socketcan.subprocess, "Popen", factory)
    yield factory
    factory.cleanup()


@pytest.fixture
def make_codec():
    created = []

    def _make(**kwargs):
        ebus = zelos_can.ExternalBus()
        codec = zelos_can.CanCodec(
            bus=ebus, source_name=f"ssh_tx_test_{next(_source_counter)}", **kwargs
        )
        created.append(codec)
        return ebus, codec

    yield _make
    for codec in created:
        with contextlib.suppress(Exception):
            codec.stop()


@pytest.fixture
def make_transport():
    created = []

    def _make(bus, channel="user@host:can0", **kwargs):
        transport = ssh_socketcan.SshTransport(bus, channel, **kwargs)
        created.append(transport)
        return transport

    yield _make
    for transport in created:
        transport.teardown()


# ── RX: fed frames decode through the real codec ─────────────────────────────


def test_rx_frames_decode_through_codec(fake_ssh, make_codec, make_transport):
    ebus, codec = make_codec(database_file=TEST_DBC)
    make_transport(ebus)

    fake_ssh.rx.feed(f"(1.0) can0 {WIRE_ID_HEX}#0011223344556677\n".encode())

    assert _wait_until(lambda: codec.metrics().messages_received >= 1)
    m = codec.metrics()
    assert m.messages_received >= 1
    assert m.messages_decoded >= 1


def test_rx_partial_line_carry(fake_ssh, make_codec, make_transport):
    ebus, codec = make_codec(database_file=TEST_DBC)
    make_transport(ebus)

    # Split a single frame across two reads; the carry buffer must reassemble.
    fake_ssh.rx.feed(f"(1.0) can0 {WIRE_ID_HEX}#0011".encode())
    time.sleep(0.05)
    fake_ssh.rx.feed(b"223344556677\n")

    assert _wait_until(lambda: codec.metrics().messages_received >= 1)


def test_rx_error_frame_not_counted(fake_ssh, make_codec, make_transport):
    """Error frames are parsed + injected but zelos-can drops them before the
    received/decoded counters, so they are invisible end to end (documented)."""
    ebus, codec = make_codec(database_file=TEST_DBC)
    make_transport(ebus)

    fake_ssh.rx.feed(b"(1.0) can0 20000004#0000000000000000\n")
    # Follow with a normal frame so we can wait on a definite signal, then
    # confirm the error frame added nothing to received.
    fake_ssh.rx.feed(f"(1.0) can0 {WIRE_ID_HEX}#0011223344556677\n".encode())

    assert _wait_until(lambda: codec.metrics().messages_received >= 1)
    time.sleep(0.2)
    assert codec.metrics().messages_received == 1  # only the normal frame


def test_rx_malformed_lines_counted_not_fatal(fake_ssh, make_codec, make_transport):
    """Unparseable lines bump _parse_drops and never wedge the reader."""
    ebus, codec = make_codec(database_file=TEST_DBC)
    transport = make_transport(ebus)

    fake_ssh.rx.feed(b"total garbage not a frame\n")
    fake_ssh.rx.feed(b"(1.0) can0 100#ZZZZ\n")  # bad hex
    fake_ssh.rx.feed(f"(1.0) can0 {WIRE_ID_HEX}#0011223344556677\n".encode())

    assert _wait_until(lambda: codec.metrics().messages_received >= 1)
    assert _wait_until(lambda: transport._parse_drops >= 2)
    assert transport.healthy is True  # dropped lines are not fatal


def test_rx_oversized_carry_dropped(fake_ssh, make_codec, make_transport):
    """A newline-less stream past the cap is dropped (bounded memory), and a
    real frame after it still decodes."""
    ebus, codec = make_codec(database_file=TEST_DBC)
    transport = make_transport(ebus)

    fake_ssh.rx.feed(b"A" * (65536 + 4096))  # no newline → overflow
    assert _wait_until(lambda: transport._overflow_drops >= 1)

    fake_ssh.rx.feed(f"(1.0) can0 {WIRE_ID_HEX}#0011223344556677\n".encode())
    assert _wait_until(lambda: codec.metrics().messages_received >= 1)
    assert transport.healthy is True


def test_stderr_flood_does_not_stall_rx(fake_ssh, make_codec, make_transport):
    """>64 KB of stderr must be drained continuously, not block the remote
    write; RX keeps flowing while stderr floods."""
    ebus, codec = make_codec(database_file=TEST_DBC)
    make_transport(ebus)

    fake_ssh.rx.feed_stderr(b"ssh noise line\n" * 8000)  # ~112 KB, > pipe buffer
    fake_ssh.rx.feed(f"(1.0) can0 {WIRE_ID_HEX}#0011223344556677\n".encode())

    assert _wait_until(lambda: codec.metrics().messages_received >= 1)


# ── TX: writer thread formats next_tx frames onto cansend stdin ──────────────


def test_writer_emits_cansend_line(fake_ssh, make_codec, make_transport):
    ebus, codec = make_codec()
    make_transport(ebus)

    codec.send(zelos_can.Message(arbitration_id=0x123, data=b"\xaa\xbb"))

    assert _wait_until(lambda: b"123#AABB\n" in fake_ssh.tx.stdin.getvalue())


def test_writer_emits_extended_frame(fake_ssh, make_codec, make_transport):
    ebus, codec = make_codec()
    make_transport(ebus)

    codec.send(zelos_can.Message(arbitration_id=0x100, data=b"\x01", is_extended_id=True))

    assert _wait_until(lambda: b"00000100#01\n" in fake_ssh.tx.stdin.getvalue())


def test_writer_emits_fd_frame_with_brs_esi(fake_ssh, make_codec, make_transport):
    """FD BRS/ESI flags survive the adapter conversion and reach the wire (flag
    nibble 0x3 = BRS|ESI)."""
    ebus, codec = make_codec()
    transport = make_transport(ebus)
    adapter = ssh_socketcan.CodecTxAdapter(codec, transport, "user@host:can0")

    adapter.send(
        can.Message(
            arbitration_id=0x321,
            data=b"\xaa\xbb",
            is_fd=True,
            bitrate_switch=True,
            error_state_indicator=True,
        )
    )

    assert _wait_until(lambda: b"321##3AABB\n" in fake_ssh.tx.stdin.getvalue())


# ── healthy transitions ──────────────────────────────────────────────────────


def test_healthy_true_when_running(fake_ssh, make_codec, make_transport):
    ebus, codec = make_codec()
    transport = make_transport(ebus)
    assert _wait_until(lambda: transport.healthy is True)


def test_healthy_false_on_stdout_eof(fake_ssh, make_codec, make_transport):
    ebus, codec = make_codec()
    transport = make_transport(ebus)
    assert _wait_until(lambda: transport.healthy is True)

    fake_ssh.rx.close_stdout()  # candump/ssh closed → reader hits EOF

    assert _wait_until(lambda: transport.healthy is False)


def test_healthy_false_on_dead_proc(fake_ssh, make_codec, make_transport):
    ebus, codec = make_codec()
    transport = make_transport(ebus)
    assert _wait_until(lambda: transport.healthy is True)

    fake_ssh.tx.die(1)  # tx ssh proc exited

    assert transport.healthy is False


def test_teardown_joins_threads(fake_ssh, make_codec):
    ebus, codec = make_codec()
    transport = ssh_socketcan.SshTransport(ebus, "user@host:can0")
    reader, writer = transport._reader, transport._writer
    rx_err, tx_err = transport._rx_err, transport._tx_err
    assert _wait_until(lambda: transport.healthy is True)

    transport.teardown()

    # All four threads joined (dead), and the refs nulled to break the cycle.
    for thread in (reader, writer, rx_err, tx_err):
        assert not thread.is_alive()
    assert transport._reader is None and transport._writer is None
    assert transport._rx_err is None and transport._tx_err is None
    assert transport.healthy is False


# ── construction failures ────────────────────────────────────────────────────


def test_partial_construction_no_leak(fake_ssh, make_codec):
    ebus, codec = make_codec()
    fake_ssh.fail_on_call = 2  # the TX Popen raises

    before = {t.ident for t in threading.enumerate()}
    with pytest.raises(can.exceptions.CanInitializationError):
        ssh_socketcan.SshTransport(ebus, "user@host:can0")

    # First (RX) proc created and torn down; no second proc; no leaked threads.
    assert len(fake_ssh.procs) == 1
    assert fake_ssh.procs[0].poll() is not None
    leaked = [t for t in threading.enumerate() if t.ident not in before and t.is_alive()]
    assert leaked == []


def test_invalid_iface_rejected(fake_ssh, make_codec):
    ebus, codec = make_codec()
    with pytest.raises(can.exceptions.CanInitializationError):
        ssh_socketcan.SshTransport(ebus, "host:can0;rm -rf")  # shell metacharacters
    with pytest.raises(can.exceptions.CanInitializationError):
        ssh_socketcan.SshTransport(ebus, "host:0can")  # must start with a letter


def test_missing_ssh_rejected(monkeypatch, make_codec):
    ebus, codec = make_codec()
    monkeypatch.setattr(ssh_socketcan.shutil, "which", lambda _name: None)
    with pytest.raises(can.exceptions.CanInterfaceNotImplementedError):
        ssh_socketcan.SshTransport(ebus, "host:can0")


def test_teardown_idempotent(fake_ssh, make_codec):
    ebus, codec = make_codec()
    transport = ssh_socketcan.SshTransport(ebus, "host:can0")
    transport.teardown()
    transport.teardown()  # second call must not raise
    assert transport.healthy is False


# ── startup connection probe ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stderr, expect",
    [
        # host key not trusted / changed
        (b"Host key verification failed.\r\n", "host key"),
        (b"@@@ REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\r\n", "host key"),
        # auth failure (BatchMode → publickey)
        (b"zelos@edge: Permission denied (publickey).\r\n", "authentication"),
        # unreachable / refused / timed out
        (b"ssh: connect to host edge port 22: Connection refused\r\n", "cannot reach"),
        (b"ssh: connect to host edge port 22: Operation timed out\r\n", "cannot reach"),
        # DNS
        (b"ssh: Could not resolve hostname edge: nodename nor servname provided\r\n", "resolve"),
        # remote is missing can-utils
        (b"bash: candump: command not found\r\n", "can-utils"),
    ],
)
def test_startup_probe_fails_fast_on_dead_rx(fake_ssh, make_codec, stderr, expect):
    """A candump proc that exits immediately (permanent ssh failure) makes the
    probe raise a classified, actionable CanInitializationError — fast, with the
    raw stderr appended — and leaks no threads."""
    ebus, codec = make_codec()
    fake_ssh.rx_dead_stderr = stderr

    before = {t.ident for t in threading.enumerate()}
    t0 = time.monotonic()
    with pytest.raises(can.exceptions.CanInitializationError) as ei:
        ssh_socketcan.SshTransport(ebus, "zelos@edge:can0")
    elapsed = time.monotonic() - t0

    msg = str(ei.value)
    assert expect in msg, f"expected {expect!r} in classified message: {msg!r}"
    assert "(ssh:" in msg  # raw stderr is always appended
    assert elapsed < 3.0  # failed fast, did not spin the full connect timeout
    # The partial transport was torn down: no leaked threads survive the raise.
    leaked = [t for t in threading.enumerate() if t.ident not in before and t.is_alive()]
    assert leaked == []


def test_startup_probe_generic_when_no_stderr(fake_ssh, make_codec, monkeypatch):
    """A dead rx proc with no stderr still fails fast, with the generic message
    and the '<no stderr>' placeholder."""
    monkeypatch.setattr(ssh_socketcan, "_STARTUP_STDERR_SETTLE", 0.05)
    ebus, codec = make_codec()
    fake_ssh.rx_dead_stderr = b""  # dies, writes nothing

    with pytest.raises(can.exceptions.CanInitializationError) as ei:
        ssh_socketcan.SshTransport(ebus, "zelos@edge:can0")
    msg = str(ei.value)
    assert "ssh-socketcan failed to start on edge:can0" in msg
    assert "<no stderr>" in msg


def test_startup_probe_succeeds_when_frames_stream(fake_ssh, make_codec, make_transport):
    """The happy path: candump streams a frame during the grace, so the probe
    connects (no raise), the transport is healthy, and the frame decodes."""
    ebus, codec = make_codec(database_file=TEST_DBC)
    fake_ssh.rx_born_frame = f"(1.0) can0 {WIRE_ID_HEX}#0011223344556677\n".encode()

    t0 = time.monotonic()
    transport = make_transport(ebus)  # must NOT raise
    elapsed = time.monotonic() - t0

    assert transport._rx_started.is_set()
    assert transport.healthy is True
    # Broke on the first frame, before the (already-short) idle grace elapsed.
    assert elapsed < ssh_socketcan._STARTUP_GRACE + 0.1
    assert _wait_until(lambda: codec.metrics().messages_received >= 1)


def test_startup_probe_succeeds_on_idle_alive_proc(fake_ssh, make_codec, make_transport):
    """An idle-but-connected bus (no frames, proc alive through the grace) is NOT
    a failure: the probe waits the grace, then proceeds with a healthy transport.
    Guards against a false-fail on a quiet bus."""
    ebus, codec = make_codec(database_file=TEST_DBC)  # no frames fed, proc stays alive

    t0 = time.monotonic()
    transport = make_transport(ebus)  # must NOT raise
    elapsed = time.monotonic() - t0

    assert not transport._rx_started.is_set()  # never streamed a frame
    assert transport.healthy is True
    # Waited ~the (shrunk) grace — not an instant fail, not a hang.
    assert 0.1 <= elapsed < 2.0


# ── argv construction ────────────────────────────────────────────────────────


def test_argv_options(fake_ssh, make_codec, make_transport):
    ebus, codec = make_codec()
    make_transport(
        ebus,
        channel="zelos@edge:can1",
        ssh_port=2222,
        ssh_key_path="/home/z/id_ed25519",
        ssh_extra_opts="-o StrictHostKeyChecking=no",
    )
    argv = fake_ssh.rx.argv
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "ConnectTimeout=10" in argv  # bounds TCP connect (ServerAlive* is post-connect)
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[argv.index("-i") + 1] == "/home/z/id_ed25519"
    assert "IdentitiesOnly=yes" in argv
    assert "StrictHostKeyChecking=no" in argv
    assert "zelos@edge" in argv
    assert "candump -L can1 &" in argv[-1]


def test_argv_omits_default_port_and_key(fake_ssh, make_codec, make_transport):
    ebus, codec = make_codec()
    make_transport(ebus, channel="host:can0")
    argv = fake_ssh.rx.argv
    assert "-p" not in argv
    assert "-i" not in argv
    assert argv[-1] == (
        "exec 3<&0; "
        "trap 'trap - EXIT TERM INT HUP; kill 0 2>/dev/null' EXIT TERM INT HUP; "
        "candump -L can0 & p=$!; "
        "{ cat <&3 >/dev/null; kill $p 2>/dev/null; } & wait $p"
    )


def test_rx_remote_command_two_way_watchdog_shape(fake_ssh, make_codec, make_transport):
    """The RX remote command must close the channel when ANY party dies.

    Structural requirements: a process-group kill (`kill 0`) armed on both
    normal exit and fatal signals (dash/ash skip the EXIT trap on untrapped
    signals), the shell parked on `wait $p` so candump's death ends the
    command, cat reading the real stdin via a pre-dup'd fd (backgrounded
    lists get /dev/null stdin), and the iface interpolated exactly once.
    """
    ebus, codec = make_codec()
    make_transport(ebus, channel="host:can2")
    cmd = fake_ssh.rx.argv[-1]
    assert "kill 0" in cmd
    assert "EXIT TERM INT HUP" in cmd  # signal-hardened trap, not EXIT-only
    assert "wait $p" in cmd  # shell lives exactly as long as candump
    assert "exec 3<&0" in cmd and "cat <&3" in cmd  # cat reads the real stdin
    assert cmd.count("can2") == 1  # iface interpolated exactly once


# ── CodecTxAdapter ───────────────────────────────────────────────────────────


def test_adapter_send_maps_runtime_error():
    class _RaisingCodec:
        def send(self, msg):
            raise RuntimeError("bus closed")

    adapter = ssh_socketcan.CodecTxAdapter(
        _RaisingCodec(), transport=None, channel_info="host:can0"
    )
    with pytest.raises(can.exceptions.CanOperationError):
        adapter.send(can.Message(arbitration_id=0x1, data=b"\x01"))


def test_adapter_send_forwards_to_codec(make_codec):
    ebus, codec = make_codec()
    adapter = ssh_socketcan.CodecTxAdapter(codec, transport=None, channel_info="host:can0")
    adapter.send(can.Message(arbitration_id=0x321, data=b"\x0a\x0b"))
    got = ebus.next_tx(timeout=1.0)
    assert got is not None
    assert got.arbitration_id == 0x321
    assert bytes(got.data) == b"\x0a\x0b"


def test_adapter_periodic_shim(make_codec):
    ebus, codec = make_codec()
    adapter = ssh_socketcan.CodecTxAdapter(codec, transport=None, channel_info="host:can0")

    shim = adapter.send_periodic(can.Message(arbitration_id=0x300, data=b"\x01"), 0.02)
    assert shim.is_active is True

    # Drain the outlet so the periodic keeps ticking (as the writer thread would).
    got = ebus.next_tx(timeout=1.0)
    assert got is not None and got.arbitration_id == 0x300

    shim.stop()
    assert _wait_until(lambda: shim.is_active is False)


def test_adapter_periodic_modify_data(make_codec):
    ebus, codec = make_codec()
    adapter = ssh_socketcan.CodecTxAdapter(codec, transport=None, channel_info="host:can0")
    shim = adapter.send_periodic(can.Message(arbitration_id=0x300, data=b"\x01"), 0.02)
    # modify_data accepts a can.Message and must not raise.
    shim.modify_data(can.Message(arbitration_id=0x300, data=b"\x02"))
    assert _wait_until(
        lambda: (msg := ebus.next_tx(timeout=0.5)) is not None and bytes(msg.data) == b"\x02"
    )
    shim.stop()


@pytest.mark.parametrize(
    "args,kwargs",
    [
        ((), {"duration": 5.0}),
        ((), {"autostart": False}),
        ((), {"modifier_callback": lambda x: x}),
    ],
)
def test_adapter_periodic_unsupported_kwargs_raise(make_codec, args, kwargs):
    ebus, codec = make_codec()
    adapter = ssh_socketcan.CodecTxAdapter(codec, transport=None, channel_info="host:can0")
    msg = can.Message(arbitration_id=0x1, data=b"\x01")
    with pytest.raises(can.exceptions.CanOperationError):
        adapter.send_periodic(msg, 0.1, *args, **kwargs)


def test_adapter_periodic_multi_message_raises(make_codec):
    ebus, codec = make_codec()
    adapter = ssh_socketcan.CodecTxAdapter(codec, transport=None, channel_info="host:can0")
    msg = can.Message(arbitration_id=0x1, data=b"\x01")
    with pytest.raises(can.exceptions.CanOperationError):
        adapter.send_periodic([msg, msg], 0.1)


def test_adapter_state_reflects_transport():
    class _FakeTransport:
        def __init__(self, healthy):
            self.healthy = healthy

    adapter = ssh_socketcan.CodecTxAdapter(
        codec=None, transport=_FakeTransport(True), channel_info="c"
    )
    assert adapter.state == can.BusState.ACTIVE

    adapter.transport = _FakeTransport(False)
    assert adapter.state == can.BusState.ERROR

    adapter.transport = None
    assert adapter.state == can.BusState.ERROR


def test_adapter_shutdown_routes_to_teardown():
    class _FakeTransport:
        def __init__(self):
            self.teardowns = 0

        def teardown(self):
            self.teardowns += 1

    transport = _FakeTransport()
    adapter = ssh_socketcan.CodecTxAdapter(codec=None, transport=transport, channel_info="c")
    adapter.shutdown()
    assert transport.teardowns == 1

    adapter.transport = None
    adapter.shutdown()  # no transport → no raise
