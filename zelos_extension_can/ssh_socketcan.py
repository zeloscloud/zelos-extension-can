"""SSH-bridged SocketCAN transport for zelos-extension-can.

Bridges a remote edge device's SocketCAN bus over ``ssh`` using the edge's own
``can-utils`` (``candump``/``cansend``) — nothing is deployed on the edge; the
local side only needs an ``ssh`` client, so this runs on Linux, macOS, and
Windows.

Two pieces:

  * :class:`SshTransport` owns two ssh processes (candump RX, cansend TX) and
    four threads (reader, writer, and one stderr-drain per proc). It moves raw
    frames between the wire and a durable ``zelos_can.ExternalBus``; decode,
    tracing, TX channel, periodics, metrics, and backpressure are the codec's
    Rust machinery. The transport is **disposable** and may be rebuilt on
    reconnect without disturbing the ``ExternalBus`` or ``CanCodec`` it feeds.
  * :class:`CodecTxAdapter` presents the small python-can-shaped surface the
    existing action layer touches (``send``/``send_periodic``/``state``/
    ``shutdown``) on top of the Rust codec + transport.

Error frames are NOT traced end to end, by design. ``candump -L <iface>``
subscribes with the default error mask (0), so the edge never emits error
frames into the RX stream in the first place; and even if one were injected,
zelos-can's decoder drops error frames before the received/decoded counters,
so they stay invisible to metrics. The transport still *parses* an error-frame
line correctly (and injects it with ``is_error_frame=True``) so the seam is
future-proof, but do not expect error frames in a trace today.
"""

import contextlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time

import can.exceptions
import zelos_can

from ._candump import format_cansend_frame, parse_candump_line, parse_ssh_channel

logger = logging.getLogger(__name__)

# Guards the interface name, which is interpolated into the remote shell
# command string (shell-injection defense).
IFACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")

_READ_CHUNK = 65536
_STDERR_CAP = 4096  # bytes of ssh stderr retained for the death-log
_STDERR_CHUNK = 4096
# Cap on an unterminated RX line. candump lines are tiny (<300 B even for a
# 64-byte FD frame); a carry that grows past this without a newline means the
# remote is streaming garbage, so drop it rather than grow toward OOM.
_MAX_LINE = 65536
_DROP_LOG_INTERVAL = 5.0  # seconds between rate-limited drop-diagnostic logs
_JOIN_TIMEOUT = 2.0
_WAIT_TIMEOUT = 2.0
_NEXT_TX_TIMEOUT = 0.5
_CONNECT_TIMEOUT = 10  # seconds for the TCP connect (ServerAlive* is post-connect only)
# Startup connection probe: how long __init__ waits for candump to prove the
# link is up (first RX frame) before assuming an idle-but-connected bus. A fast
# ssh failure (host key / auth / bad remote command) dies in <2 s, well inside
# this grace, so the probe catches it and fails fast; a slow unreachable host
# (full ConnectTimeout) outlives the grace and is left to the reconnect
# supervisor. See the probe block at the end of __init__.
_STARTUP_GRACE = 3.0
# When the probe finds candump already dead, wait up to this long for the stderr
# drain thread to record the exit reason before classifying it (the proc's
# stderr is fully buffered once it exits; this only closes the drain-vs-probe
# scheduling race and only ever runs on the failure path).
_STARTUP_STDERR_SETTLE = 0.25


def _classify_ssh_failure(
    host: str, iface: str, ssh_port: int, stderr_tail: str
) -> can.exceptions.CanInitializationError:
    """Turn an ssh startup-failure stderr tail into an actionable error.

    Case-insensitive substring match on the last bytes ssh/candump wrote before
    exiting. Every message names the concrete fix and appends the raw stderr so
    the underlying cause is never lost.
    """
    low = stderr_tail.lower()
    suffix = f" (ssh: {stderr_tail or '<no stderr>'})"

    def has(*needles: str) -> bool:
        return any(n in low for n in needles)

    if has("host key verification failed", "remote host identification has changed"):
        msg = (
            f"ssh host key for {host} is not trusted (or has changed). Fix: run "
            f"`ssh-keyscan -H {host} >> ~/.ssh/known_hosts`, or connect once by hand "
            f"with `ssh {host}` to accept it, or add "
            "`-o StrictHostKeyChecking=accept-new` to ssh_extra_opts. The extension "
            "runs non-interactively, so it cannot prompt to accept a new key."
        )
    elif has("permission denied", "publickey", "password"):
        msg = (
            f"ssh authentication to {host} failed. The extension runs with BatchMode "
            "(no password prompt), so authorize your key with `ssh-copy-id`, or set "
            "ssh_key_path, or use an ssh-agent."
        )
    elif has("could not resolve", "name or service not known", "nodename nor servname"):
        msg = f"cannot resolve host {host}; check the remote_host value and your DNS."
    elif has(
        "connection refused",
        "connection timed out",
        "no route to host",
        "operation timed out",
    ):
        msg = (
            f"cannot reach {host}:{ssh_port}; check that the host is up and that "
            "ssh_port is correct."
        )
    elif has("candump: not found", "cansend: not found", "command not found"):
        msg = f"the edge {host} is missing can-utils (candump/cansend); install can-utils on it."
    else:
        msg = f"ssh-socketcan failed to start on {host}:{iface}."

    return can.exceptions.CanInitializationError(msg + suffix)


class SshTransport:
    """Owns the ssh procs + reader/writer threads for one remote CAN bus.

    Disposable: the :class:`zelos_can.ExternalBus` handed in is durable and
    outlives transport rebuilds. Any failure during construction tears down
    partial state and raises so no proc/thread is ever leaked.
    """

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
        self._bus = bus
        self.channel = channel
        self._fd_mode = fd_mode
        self._stop = threading.Event()
        self._eof = False
        self._rx_proc: subprocess.Popen | None = None
        self._tx_proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._rx_err: threading.Thread | None = None
        self._tx_err: threading.Thread | None = None
        # Set by the reader thread on the FIRST non-empty candump chunk: proof
        # the ssh link is up and streaming. The startup probe waits on it.
        self._rx_started = threading.Event()
        # Last _STDERR_CAP bytes of each proc's stderr, kept live by the drain
        # threads so the startup probe / reconnect supervisor can read WHY a
        # link failed (host key / auth / unreachable) instead of guessing.
        self._rx_stderr_tail = b""
        self._tx_stderr_tail = b""
        # Observability for dropped RX lines (parse failures and oversized
        # carry). Public attributes so the codec/supervisor can surface them.
        self._parse_drops = 0
        self._overflow_drops = 0
        self._last_drop_log = 0.0

        if shutil.which("ssh") is None:
            raise can.exceptions.CanInterfaceNotImplementedError(
                "ssh client not found on PATH; ssh-socketcan requires an ssh binary"
            )

        user, host, iface = parse_ssh_channel(channel)
        if not IFACE_RE.match(iface):
            raise can.exceptions.CanInitializationError(
                f"invalid CAN interface name {iface!r} (must match {IFACE_RE.pattern})"
            )
        self._iface = iface

        # Discard any periodic backlog left in the outlet by a prior transport.
        bus.drain_tx()

        try:
            base_argv = self._build_argv(user, host, ssh_port, ssh_key_path, ssh_extra_opts)

            # RX: candump + a two-way watchdog. The guarantee is symmetric —
            # if ANY party dies (us, candump, or the wrapper shell), every
            # remote holder of the ssh channel dies too, so the local reader
            # always sees EOF instead of a silent, healthy-looking RX starve.
            #
            # Construction notes (verified under /bin/sh, dash, and bash):
            #  * `exec 3<&0` dups the real channel stdin to fd 3 BEFORE the
            #    watchdog is backgrounded. This is load-bearing: POSIX assigns
            #    /dev/null as stdin to backgrounded lists in non-interactive
            #    shells, so a bare `{ cat >/dev/null; } &` EOFs instantly and
            #    would kill candump at startup. `cat <&3` reads the real stdin.
            #  * The trap covers TERM/INT/HUP as well as EXIT because dash and
            #    busybox-ash do NOT run the EXIT trap on an untrapped fatal
            #    signal; `trap - ...` first prevents handler re-entry when
            #    `kill 0` TERMs the shell itself.
            #  * `kill 0` signals the whole remote process group (candump, the
            #    watchdog subshell, cat, shell) — non-interactive shells keep
            #    background jobs in the shell's own group.
            #
            # Death paths:
            #  1. Local teardown (we close stdin) -> cat sees EOF -> watchdog
            #     kills candump -> `wait $p` returns -> shell exits -> trap
            #     `kill 0` reaps the watchdog subshell -> no channel-fd holder
            #     left -> channel closes. Orphan-safe, as before.
            #  2. candump dies (crash, pkill, iface down) -> `wait $p` returns
            #     immediately -> trap `kill 0` kills cat + watchdog -> channel
            #     closes -> local reader gets EOF -> `_eof=True` -> `healthy`
            #     False -> supervisor rebuilds the transport. (The two-way
            #     guarantee the old cat-only watchdog lacked.)
            #  3. Wrapper shell killed externally -> TERM/HUP trap fires (EXIT
            #     alone is not enough on dash/ash) -> `kill 0` nukes candump +
            #     cat -> channel closes -> EOF.
            rx_cmd = (
                "exec 3<&0; "
                "trap 'trap - EXIT TERM INT HUP; kill 0 2>/dev/null' EXIT TERM INT HUP; "
                f"candump -L {iface} & p=$!; "
                "{ cat <&3 >/dev/null; kill $p 2>/dev/null; } & wait $p"
            )
            self._rx_proc = subprocess.Popen(
                base_argv + [rx_cmd],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            # TX: read frames on stdin, cansend each. stdin EOF ends the loop
            # (orphan-safe). The TX side has no equivalent of the RX detection
            # gap: the read-loop IS the remote command (no background child to
            # orphan), so if it dies the command exits, sshd closes the
            # session, the local ssh client exits, and `proc.poll()` goes
            # non-None -> `healthy` False. If the pipe breaks mid-write, the
            # writer thread gets BrokenPipeError/OSError -> `_eof=True`. A
            # failing `cansend` (iface down) is deliberately tolerated
            # (`2>/dev/null`, loop continues); that surfaces on the RX side
            # instead, where candump on a dead iface exits -> path 2 above.
            tx_cmd = f'while IFS= read -r f; do cansend {iface} "$f" 2>/dev/null; done'
            self._tx_proc = subprocess.Popen(
                base_argv + [tx_cmd],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            self._reader = threading.Thread(
                target=self._read_loop, name=f"ssh-can-rx-{iface}", daemon=False
            )
            self._writer = threading.Thread(
                target=self._write_loop, name=f"ssh-can-tx-{iface}", daemon=False
            )
            # Continuous stderr drains: without them >64 KB of ssh/candump
            # stderr fills the pipe, blocks the remote write, and stalls RX/TX
            # while `healthy` still reads True. Daemon so they can never wedge
            # teardown; each keeps only the last few KB and logs it on EOF.
            self._rx_err = threading.Thread(
                target=self._drain_stderr,
                args=(self._rx_proc, "candump", "_rx_stderr_tail"),
                name=f"ssh-can-rx-err-{iface}",
                daemon=True,
            )
            self._tx_err = threading.Thread(
                target=self._drain_stderr,
                args=(self._tx_proc, "cansend", "_tx_stderr_tail"),
                name=f"ssh-can-tx-err-{iface}",
                daemon=True,
            )
            self._reader.start()
            self._writer.start()
            self._rx_err.start()
            self._tx_err.start()
        except Exception as e:
            self._teardown()
            raise can.exceptions.CanInitializationError(
                f"failed to start ssh-socketcan transport on {channel!r}: {e}"
            ) from e

        # ── Startup connection probe ─────────────────────────────────────────
        # A false-fail is IMPOSSIBLE here: we only raise if the rx (candump)
        # proc actually EXITED. An idle-but-connected bus keeps candump alive and
        # simply proceeds. Fast failures (host key / auth / bad remote command)
        # die in <2 s, so the 3 s grace catches them and fails fast; a slow
        # unreachable host (full ConnectTimeout=10 s) outlives the grace,
        # proceeds, and is handled by the codec's reconnect supervisor.
        deadline = time.monotonic() + _STARTUP_GRACE
        while time.monotonic() < deadline:
            if self._rx_started.is_set():
                break  # candump is streaming — connected
            if self._rx_proc.poll() is not None:
                # candump exited before streaming a frame. Give the stderr drain
                # a brief moment to record the exit reason, then classify it into
                # an actionable error and tear the partial transport down.
                settle = time.monotonic() + _STARTUP_STDERR_SETTLE
                while time.monotonic() < settle and not self._rx_stderr_tail:
                    time.sleep(0.01)
                tail = bytes(self._rx_stderr_tail).decode("utf-8", "replace").strip()
                self._teardown()
                raise _classify_ssh_failure(host, iface, ssh_port, tail)
            time.sleep(0.05)
        # Grace elapsed with candump still alive: idle-but-connected bus, or a
        # slow-but-valid connect. Assume connected and proceed.

    @staticmethod
    def _build_argv(user, host, ssh_port, ssh_key_path, ssh_extra_opts) -> list[str]:
        argv = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            # ConnectTimeout bounds the TCP handshake; ServerAlive* only apply
            # AFTER a session is established, so without this an unreachable
            # (packet-dropping) host would sit in connect() for ~75-130 s while
            # `healthy` still read True.
            "-o",
            f"ConnectTimeout={_CONNECT_TIMEOUT}",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if ssh_port != 22:
            argv += ["-p", str(ssh_port)]
        if ssh_key_path:
            argv += ["-i", ssh_key_path, "-o", "IdentitiesOnly=yes"]
        argv += shlex.split(ssh_extra_opts or "")
        argv.append(f"{user}@{host}" if user else host)
        return argv

    # ── Threads ──────────────────────────────────────────────────────────

    def _read_loop(self) -> None:
        """Stream candump stdout → parse → ``bus.inject`` (channel-backpressured).

        Uses ``os.read`` + a carry buffer (never readline/select). An empty
        read is EOF; a ``RuntimeError`` from ``inject`` means the codec stopped
        and dropped the bus, so we wind the thread down. Unparseable lines and
        an oversized (newline-less) carry are dropped and counted, not fatal.
        """
        fd = self._rx_proc.stdout.fileno()
        carry = b""
        while not self._stop.is_set():
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except OSError:
                self._eof = True
                break
            if not chunk:
                self._eof = True
                break
            # First bytes off candump prove the ssh link is up and streaming;
            # signal the startup probe (idempotent — set() is a no-op after).
            if not self._rx_started.is_set():
                self._rx_started.set()
            carry += chunk
            while True:
                nl = carry.find(b"\n")
                if nl < 0:
                    break
                line, carry = carry[:nl], carry[nl + 1 :]
                frame = parse_candump_line(line)
                if frame is None:
                    self._parse_drops += 1
                    self._maybe_log_drops()
                    continue
                try:
                    self._bus.inject(
                        frame.arb_id,
                        frame.data,
                        timestamp=frame.timestamp,
                        is_extended=frame.is_extended,
                        is_fd=frame.is_fd,
                        is_remote_frame=frame.is_remote,
                        is_error_frame=frame.is_error,
                        bitrate_switch=frame.brs,
                        error_state_indicator=frame.esi,
                    )
                except RuntimeError:
                    return
            # Bound the carry: a newline-less buffer past the cap is a garbage
            # stream, not a real candump line. Drop it so we can't OOM.
            if len(carry) > _MAX_LINE:
                self._overflow_drops += 1
                self._maybe_log_drops()
                carry = b""

    def _write_loop(self) -> None:
        """Drain ``bus.next_tx`` → ``format_cansend_frame`` → write to cansend stdin.

        A ``RuntimeError`` from ``next_tx`` means the codec dropped the bus;
        ``BrokenPipeError``/``OSError`` means the ssh pipe died. Either ends the
        thread — frames then pool harmlessly in the bounded outlet.
        """
        stdin = self._tx_proc.stdin
        while not self._stop.is_set():
            try:
                frame = self._bus.next_tx(timeout=_NEXT_TX_TIMEOUT)
            except RuntimeError:
                return
            if frame is None:
                continue
            line = format_cansend_frame(frame)
            try:
                stdin.write(line.encode() + b"\n")
                stdin.flush()
            except (BrokenPipeError, OSError):
                self._eof = True
                return

    def _maybe_log_drops(self) -> None:
        """Rate-limited debug log so a mis-shapen remote is diagnosable."""
        now = time.monotonic()
        if now - self._last_drop_log < _DROP_LOG_INTERVAL:
            return
        self._last_drop_log = now
        logger.debug(
            "ssh-socketcan (%s) dropped RX lines: parse=%d oversized=%d",
            self.channel,
            self._parse_drops,
            self._overflow_drops,
        )

    def _drain_stderr(self, proc: subprocess.Popen | None, label: str, tail_attr: str) -> None:
        """Continuously drain a proc's stderr into a bounded ring; log on EOF.

        Draining prevents the >64 KB pipe-fill deadlock that would otherwise
        stall the remote write. Only the last ``_STDERR_CAP`` bytes are kept,
        and they are logged once at WARNING when the pipe EOFs (proc died) —
        but not during an intentional teardown, where ssh's "Killed by signal"
        noise is expected rather than diagnostic.

        The live tail is mirrored onto ``self.<tail_attr>`` (an immutable
        ``bytes`` snapshot) after every read so the startup probe and reconnect
        supervisor can read WHY a link failed without racing this thread.
        """
        if proc is None or proc.stderr is None:
            return
        fd = proc.stderr.fileno()
        ring = bytearray()
        while True:
            try:
                chunk = os.read(fd, _STDERR_CHUNK)
            except OSError:
                break
            if not chunk:
                break
            ring += chunk
            if len(ring) > _STDERR_CAP:
                del ring[: len(ring) - _STDERR_CAP]
            # Publish an immutable snapshot; attribute assignment is atomic, so a
            # concurrent reader always sees a consistent (if slightly stale) tail.
            setattr(self, tail_attr, bytes(ring))
        if ring and not self._stop.is_set():
            logger.warning(
                "ssh-socketcan (%s) %s stderr: %s",
                self.channel,
                label,
                ring.decode("utf-8", "replace").strip(),
            )

    # ── State / teardown ─────────────────────────────────────────────────

    @property
    def healthy(self) -> bool:
        """True iff both procs are running and both threads alive; TOTAL."""
        try:
            if self._eof:
                return False
            for proc in (self._rx_proc, self._tx_proc):
                if proc is None or proc.poll() is not None:
                    return False
            for thread in (self._reader, self._writer):
                if thread is None or not thread.is_alive():
                    return False
            return True
        except Exception:
            return False

    def stderr_tail(self) -> str:
        """Decoded tail of the rx (candump) stderr — the last diagnostic bytes
        ssh/candump wrote. Empty when there is none. The reconnect supervisor
        logs this so an unhealthy link reads as "unreachable"/"timed out" rather
        than a bare "unhealthy"."""
        return bytes(self._rx_stderr_tail).decode("utf-8", "replace").strip()

    def teardown(self) -> None:
        """Idempotent, orphan-safe, best-effort teardown (never raises)."""
        self._teardown()

    def _teardown(self) -> None:
        self._stop.set()
        # Close stdins first: RX close → cat EOF → trap reaps candump;
        # TX close → read-loop EOF.
        for proc in (self._rx_proc, self._tx_proc):
            if proc is None or proc.stdin is None:
                continue
            with contextlib.suppress(Exception):
                proc.stdin.close()
        # terminate → proc death → stdout/stderr EOF → every thread unblocks.
        for proc in (self._rx_proc, self._tx_proc):
            if proc is None:
                continue
            with contextlib.suppress(Exception):
                proc.terminate()
        # Join ALL threads (reader, writer, both stderr drains) before closing
        # their fds, so no thread is mid-read on an fd we close (fd-reuse race).
        for thread in (self._reader, self._writer, self._rx_err, self._tx_err):
            if thread is None:
                continue
            with contextlib.suppress(Exception):
                thread.join(timeout=_JOIN_TIMEOUT)
        for proc in (self._rx_proc, self._tx_proc):
            if proc is None:
                continue
            try:
                proc.wait(timeout=_WAIT_TIMEOUT)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
        # Close the stdout/stderr pipe fds — Popen.__del__ would eventually,
        # but a flapping link rebuilds often enough to march toward EMFILE
        # before GC runs. stdin is already closed above.
        for proc in (self._rx_proc, self._tx_proc):
            if proc is None:
                continue
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()
        # Break the Thread→bound-method→self reference cycle so this disposable
        # transport refcounts away promptly instead of waiting for the cyclic GC.
        self._reader = self._writer = self._rx_err = self._tx_err = None


def _to_zelos_message(msg) -> zelos_can.Message:  # noqa: ANN001 — duck-typed can.Message
    """Convert a python-can ``Message`` into a ``zelos_can.Message``.

    Carries the FD bit-rate-switch / error-state-indicator flags through so an
    FD TX frame keeps its BRS/ESI (``format_cansend_frame`` renders them).
    """
    return zelos_can.Message(
        arbitration_id=msg.arbitration_id,
        data=bytes(msg.data),
        is_extended_id=msg.is_extended_id,
        is_fd=msg.is_fd,
        is_remote_frame=msg.is_remote_frame,
        bitrate_switch=msg.bitrate_switch,
        error_state_indicator=msg.error_state_indicator,
    )


class _PeriodicShim:
    """Wraps a Rust ``CyclicSendTask`` so the action layer sees a python-can-ish
    task (``stop()``, ``modify_data(msg)``, ``is_active``) that accepts
    ``can.Message`` inputs."""

    def __init__(self, task):
        self._task = task

    def stop(self) -> None:
        self._task.stop()

    def modify_data(self, msg) -> None:  # noqa: ANN001 — duck-typed can.Message
        self._task.modify_data(_to_zelos_message(msg))

    @property
    def is_active(self) -> bool:
        return self._task.is_active


class CodecTxAdapter:
    """python-can-shaped TX surface over a ``zelos_can.CanCodec`` + transport.

    Covers exactly the surface the action layer in ``codec.py`` touches:
    ``send``, ``send_periodic``, ``state``, ``shutdown``. ``transport`` is a
    mutable attribute the owner swaps on reconnect (the codec and its
    ExternalBus persist).
    """

    def __init__(self, codec, transport, channel_info):
        self._codec = codec
        self.transport = transport
        self.channel_info = channel_info

    def send(self, msg, timeout=None) -> None:
        try:
            self._codec.send(_to_zelos_message(msg))
        except RuntimeError as e:
            raise can.exceptions.CanOperationError(str(e)) from e

    def send_periodic(
        self,
        msgs,
        period,
        duration=None,
        autostart=True,
        modifier_callback=None,
    ):
        # Only the single-message, autostart-now path is supported; the codec's
        # _spawn_periodic never asks for anything else. Fail loud rather than
        # silently degrade.
        if duration is not None or not autostart or modifier_callback is not None:
            raise can.exceptions.CanOperationError(
                "send_periodic: only single-message autostart is supported on ssh-socketcan"
            )
        msg = msgs
        if isinstance(msgs, (list, tuple)):
            if len(msgs) != 1:
                raise can.exceptions.CanOperationError(
                    "send_periodic: only a single message is supported on ssh-socketcan"
                )
            msg = msgs[0]
        try:
            task = self._codec.send_periodic(_to_zelos_message(msg), period)
        except RuntimeError as e:
            raise can.exceptions.CanOperationError(str(e)) from e
        return _PeriodicShim(task)

    @property
    def state(self):
        if self.transport is not None and self.transport.healthy:
            return can.BusState.ACTIVE
        return can.BusState.ERROR

    def shutdown(self) -> None:
        if self.transport is not None:
            self.transport.teardown()
