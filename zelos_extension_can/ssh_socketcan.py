"""SSH-bridged SocketCAN transport for zelos-extension-can.

Bridges a remote edge device's SocketCAN bus over ``ssh`` using the edge's own
``can-utils`` (``candump``/``cansend``) — nothing is deployed on the edge; the
local side only needs an ``ssh`` client, so this runs on Linux, macOS, and
Windows.

Two pieces:

  * :class:`SshTransport` owns ONE ssh process (``candump`` RX on the channel's
    stdout, a ``cansend`` read-loop consuming its stdin) and three threads
    (reader, writer, stderr drain). It moves raw frames between the wire and a
    durable ``zelos_can.ExternalBus``; decode, tracing, TX channel, periodics,
    metrics, and backpressure are the codec's Rust machinery. The transport is
    **disposable** and may be rebuilt on reconnect without disturbing the
    ``ExternalBus`` or ``CanCodec`` it feeds.
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
import sys
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
# When the probe finds the session already dead, how long it waits for the
# stderr drain to reach EOF (the proc has exited, so its stderr pipe drains and
# the drain thread returns) before classifying. Failure path only.
_STARTUP_STDERR_SETTLE = 0.25


class SshPermanentError(can.exceptions.CanInitializationError):
    """An ssh failure retrying cannot fix, as judged from stderr: auth denied,
    host key rejected under the ``strict`` policy, no ``can-utils`` on the edge.
    See :func:`_classify_ssh_failure`."""


# ssh's host-key banner is ~4 KB of boilerplate wrapped around two useful lines
# (the key fingerprint and the final cause). Lines starting with '@' — the @@@@
# rules and the WARNING band — plus these needles are dropped before anything is
# logged or appended to an error.
_BANNER_NEEDLES = (
    "it is possible",
    "someone could be eavesdropping",
    "please contact your system administrator",
    "add correct host key",
    "remove with:",
    "offending",
)
_TAIL_CAP = 200  # chars of stderr appended to a reported error
# "Offending ECDSA key in /home/u/.ssh/known_hosts:12" — the one banner line
# worth keeping, quoted by the host-key message instead of dumped with the rest.
_OFFENDING_RE = re.compile(r"Offending [\w-]+ key in (\S+)")


def _clean_stderr_tail(tail: str) -> str:
    """Strip ssh's banner boilerplate, keeping the fingerprint and the cause.

    Collapses to a single line and keeps only the LAST ``_TAIL_CAP`` chars (on a
    word boundary), so the cause — always written last — survives the cap.
    """
    kept = [
        s
        for line in tail.splitlines()
        if (s := line.strip())
        and not s.startswith("@")
        and not any(n in s.lower() for n in _BANNER_NEEDLES)
    ]
    out = " ".join(kept)
    if len(out) <= _TAIL_CAP:
        return out
    return "..." + out[-(_TAIL_CAP - 3) :].split(" ", 1)[-1]


def _auth_remedy(target: str, ssh_port: int, ssh_key_path: str | None) -> str:
    """Copy-paste command that authorizes this machine's key on the edge.

    Resolved for this bus (user, host, port, key) and for the platform the
    extension is running on — Windows has no ``ssh-copy-id``.
    """
    port = f" -p {ssh_port}" if ssh_port != 22 else ""
    if sys.platform.startswith("win"):
        # cmd.exe form (Windows has no ssh-copy-id); the public key sits next to
        # the configured private key, or at the default path when none is set.
        pub = f"{ssh_key_path}.pub" if ssh_key_path else "%USERPROFILE%\\.ssh\\id_ed25519.pub"
        return (
            "run `ssh-keygen -t ed25519` (skip if you already have a key), then in cmd.exe "
            f"`type {pub} | ssh{port} {target} "
            '"mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"`'
        )
    key = f" -i {ssh_key_path}" if ssh_key_path else ""
    return f"run `ssh-copy-id{key}{port} {target}`"


def _classify_ssh_failure(
    host: str,
    iface: str,
    ssh_port: int,
    stderr_tail: str,
    *,
    user: str | None = None,
    ssh_key_path: str | None = None,
    streamed: bool = False,
) -> can.exceptions.CanInitializationError:
    """Turn an ssh failure's stderr tail into a classified, actionable error.

    Case-insensitive substring match on the last bytes ssh/candump wrote. The
    returned CLASS is the verdict, and it is the ONLY channel that carries it:
    :class:`SshPermanentError` means an operator must act, so the app layer logs
    it once and exits; a plain ``CanInitializationError`` is transient and worth
    a reconnect — including the default case, so an unrecognized failure keeps
    retrying.

    ``streamed`` (this session got at least one frame out) vetoes every permanent
    verdict: the ring holds the WHOLE session, so a chatty login script or a
    host-key banner ssh printed and then connected through anyway would
    otherwise make a later transient drop look unfixable. Every permanent cause
    fires before the first frame. Matching runs on the RAW tail; only the
    appended copy is banner-stripped.
    """
    low = stderr_tail.lower()
    target = f"{user}@{host}" if user else host
    permanent = False

    def has(*needles: str) -> bool:
        return any(n in low for n in needles)

    if has("host key verification failed", "remote host identification has changed"):
        permanent = True
        offending = _OFFENDING_RE.search(stderr_tail)
        where = f" (old key at {offending.group(1)})" if offending else ""
        msg = (
            f"ssh host key for {host} is not trusted, or has changed — a reimaged device "
            f"presents a new one{where}. Fix: in your own terminal run "
            f"`ssh-keygen -R {host}`, accept the new key with `ssh {target}`, then restart "
            'this bus; or set this bus\'s SSH Host Key Policy to "auto" to trust whatever '
            "key the device presents."
        )
    elif has("permission denied"):  # ssh's auth failure line always says this
        permanent = True
        msg = (
            f"ssh authentication to {host} failed. BatchMode means the extension can never "
            f"prompt for a password. Fix: in your own terminal, "
            f"{_auth_remedy(target, ssh_port, ssh_key_path)} once, then restart this bus."
        )
    elif has("candump: not found", "cansend: not found", "command not found"):
        permanent = True
        msg = f"the edge {host} is missing can-utils (candump/cansend); install can-utils on it."
    elif has("siocgifindex", "no such device"):
        # After a reboot sshd can be up before can0 is configured.
        msg = (
            f"the edge {host} has no CAN interface {iface} (yet); retrying — if it never "
            "appears, check remote_channel and `ip link` on the edge."
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
    else:
        msg = f"ssh-socketcan on {host}:{iface} failed."

    suffix = f" (ssh: {_clean_stderr_tail(stderr_tail) or '<no stderr>'})"
    cls = SshPermanentError if permanent and not streamed else can.exceptions.CanInitializationError
    return cls(msg + suffix)


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
        ssh_host_key_policy="auto",
        fd_mode=False,
    ):
        self._bus = bus
        self.channel = channel
        self._fd_mode = fd_mode
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._err: threading.Thread | None = None
        # Set by the reader thread on the FIRST non-empty candump chunk: proof
        # the ssh link is up and streaming. The startup probe waits on it.
        self._rx_started = threading.Event()
        # Last _STDERR_CAP bytes of the session's stderr, kept live by the drain
        # thread so the startup probe / reconnect supervisor can read WHY a link
        # failed (host key / auth / unreachable) instead of guessing.
        self._stderr_tail = b""
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
        self._user, self._host, self._iface = user, host, iface
        self._ssh_port = ssh_port
        self._ssh_key_path = ssh_key_path

        # Discard any periodic backlog left in the outlet by a prior transport.
        bus.drain_tx()

        try:
            argv = self._build_argv(
                user, host, ssh_port, ssh_key_path, ssh_extra_opts, ssh_host_key_policy
            )
            self._proc = subprocess.Popen(
                argv + [self._remote_command(iface)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            self._reader = threading.Thread(
                target=self._read_loop, name=f"ssh-can-rx-{iface}", daemon=False
            )
            self._writer = threading.Thread(
                target=self._write_loop, name=f"ssh-can-tx-{iface}", daemon=False
            )
            # Continuous stderr drain: without it >64 KB of ssh/candump stderr
            # fills the pipe, blocks the remote write, and stalls RX/TX while
            # `healthy` still reads True. Daemon so it can never wedge teardown;
            # keeps only the last few KB and reports it once on EOF.
            self._err = threading.Thread(
                target=self._drain_stderr, name=f"ssh-can-err-{iface}", daemon=True
            )
            self._reader.start()
            self._writer.start()
            self._err.start()
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
            if self._proc.poll() is not None:
                # The session exited before streaming a frame. Wait for the whole
                # reason, not the first chunk of it: the proc is gone, so its
                # stderr pipe drains to EOF and the drain thread returns. ssh
                # writes the CAUSE last (the host-key banner runs 4 KB ahead of
                # it), so a non-empty ring proves nothing on its own.
                self._err.join(timeout=_STARTUP_STDERR_SETTLE)
                failure = self.classify_failure()
                self._teardown()
                raise failure
            time.sleep(0.05)
        # Grace elapsed with candump still alive: idle-but-connected bus, or a
        # slow-but-valid connect. Assume connected and proceed.

    @staticmethod
    def _remote_command(iface: str) -> str:
        """The single remote shell running BOTH directions, dying as one unit.

        ``candump`` streams RX on the channel's stdout; the TX read-loop is the
        shell's OWN foreground body, so either side's death ends the session and
        the local reader sees EOF (a backgrounded read-loop could die alone and
        silently swallow every TX frame).

        Load-bearing details (executed under sh/dash/bash by
        ``tests/test_ssh_remote_shell.py``):
         * ``exec 3<&0`` dups the real channel stdin before anything is
           backgrounded: POSIX gives backgrounded lists /dev/null as stdin.
         * The trap covers TERM/INT/HUP as well as EXIT — dash and busybox ash
           skip the EXIT trap on an untrapped fatal signal; ``trap - ...`` first
           blocks re-entry when ``kill 0`` TERMs this shell.
         * ``kill 0`` reaps candump AND anything it spawned, so the session must
           OWN its process group. Not every server gives it one: under Tailscale
           SSH (``tailscaled be-child ssh``) every session of every user runs in
           the daemon's group, where one session's teardown reaped the other
           buses' candumps and the operator's unrelated sessions. OpenSSH's sshd
           ``setsid``s each command, which is why only the field saw this.
         * ``kill -0 -$$`` asks whether a process group with our pid exists, i.e.
           whether we lead our own group (a pgid IS its leader's pid). Only when
           we do not is ``setsid`` wanted, and that is exactly the case where
           util-linux and busybox ``setsid`` both exec in place rather than fork
           — so the server keeps one child holding one stdin/stdout, and no
           parent exits early and closes the channel under us. Without ``setsid``
           on the edge we are no worse off than before.
         * ``exec`` keeps it all one process, so ``$$`` is this same shell in
           either branch, and ``reap`` is a function only to keep the command
           free of nested single quotes.

        Death paths, each ending in a closed channel -> local EOF:
         1. We close stdin -> ``read`` EOFs -> EXIT trap -> ``kill 0`` reaps candump.
         2. candump exits -> ``kill -TERM $$`` -> the trap interrupts ``read`` ->
            ``kill 0``.
         3. Shell signalled from outside -> TERM/HUP trap -> ``kill 0`` reaps candump.
        """
        session = (
            "exec 3<&0; "
            "reap() { trap - EXIT TERM INT HUP; kill 0 2>/dev/null; }; "
            "trap reap EXIT TERM INT HUP; "
            f"{{ candump -L {iface}; kill -TERM $$; }} & "
            f'while IFS= read -r f; do cansend {iface} "$f" >/dev/null 2>&1; done <&3'
        )
        # Single-quoted: every expansion above belongs to the shell that runs it.
        return (
            f"c='{session}'; "
            "if ! kill -0 -$$ 2>/dev/null && command -v setsid >/dev/null 2>&1; "
            'then exec setsid sh -c "$c"; else exec sh -c "$c"; fi'
        )

    @staticmethod
    def _build_argv(
        user, host, ssh_port, ssh_key_path, ssh_extra_opts, ssh_host_key_policy="auto"
    ) -> list[str]:
        # ssh honours the FIRST occurrence of an option (`ssh -o X=yes -o
        # X=accept-new -G host` reports yes), so the operator's extra opts are
        # spliced FIRST: appended last they could never override the host-key
        # policy, BatchMode, or ConnectTimeout.
        argv = [
            "ssh",
            "-T",
            *shlex.split(ssh_extra_opts or ""),
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
        # "auto" trusts whatever key the device presents and records nothing, so a
        # reimaged edge (new host key) reconnects with no manual step. "strict"
        # defers to ssh's own ~/.ssh/known_hosts, where an unknown or changed key
        # is a permanent failure. BatchMode stays either way: the transport is
        # key-only and can never answer a prompt.
        if ssh_host_key_policy == "strict":
            argv += ["-o", "StrictHostKeyChecking=yes"]
        else:
            argv += [
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                # /etc/ssh/ssh_known_hosts too: a stale system-wide entry makes
                # ssh print the CHANGED banner on a connection it then allows,
                # and that banner sits in the stderr ring misleading diagnosis.
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                # Silences the "Warning: Permanently added ..." this policy would
                # emit on EVERY connect (it is INFO); real failures are ERROR.
                "-o",
                "LogLevel=ERROR",
            ]
        if ssh_port != 22:
            argv += ["-p", str(ssh_port)]
        if ssh_key_path:
            argv += ["-i", ssh_key_path, "-o", "IdentitiesOnly=yes"]
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
        fd = self._proc.stdout.fileno()
        carry = b""
        while not self._stop.is_set():
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except OSError:
                break
            if not chunk:
                break  # EOF
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
        stdin = self._proc.stdin
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

    def _drain_stderr(self) -> None:
        """Drain the session's stderr into a bounded ring; log NOTHING.

        Draining prevents the >64 KB pipe-fill deadlock that would stall the
        remote write. Logging here too put the same 4 KB banner in the log three
        times — the party that ACTS on the failure reports it (the probe raises,
        the supervisor logs). Publishes an immutable ``bytes`` snapshot after
        every read, so the probe/supervisor never races this thread.
        """
        proc = self._proc
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
            self._stderr_tail = bytes(ring)

    # ── State / teardown ─────────────────────────────────────────────────

    @property
    def healthy(self) -> bool:
        """True iff the ssh proc is running and both RX/TX threads alive; TOTAL.

        Every EOF / broken-pipe path returns from its thread, so the liveness
        check below covers them.
        """
        try:
            if self._proc is None or self._proc.poll() is not None:
                return False
            for thread in (self._reader, self._writer):
                if thread is None or not thread.is_alive():
                    return False
            return True
        except Exception:
            return False

    def stderr_tail(self) -> str:
        """The session's last diagnostic bytes, banner-stripped ("" when silent).

        ssh's 4 KB host-key banner reduces to the fingerprint plus the final
        cause.
        """
        return _clean_stderr_tail(bytes(self._stderr_tail).decode("utf-8", "replace"))

    def classify_failure(self) -> can.exceptions.CanInitializationError:
        """Why this link failed; see :func:`_classify_ssh_failure` for the verdict."""
        return _classify_ssh_failure(
            self._host,
            self._iface,
            self._ssh_port,
            bytes(self._stderr_tail).decode("utf-8", "replace").strip(),
            user=self._user,
            ssh_key_path=self._ssh_key_path,
            streamed=self._rx_started.is_set(),
        )

    def teardown(self) -> None:
        """Idempotent, orphan-safe, best-effort teardown (never raises)."""
        self._teardown()

    def _teardown(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            # Close stdin first: the remote read loop EOFs → its shell exits →
            # the EXIT trap reaps the rest of the remote process group.
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            # terminate → proc death → stdout/stderr EOF → every thread unblocks.
            with contextlib.suppress(Exception):
                proc.terminate()
        # Join ALL threads (reader, writer, stderr drain) before closing their
        # fds, so no thread is mid-read on an fd we close (fd-reuse race).
        for thread in (self._reader, self._writer, self._err):
            if thread is None:
                continue
            with contextlib.suppress(Exception):
                thread.join(timeout=_JOIN_TIMEOUT)
        if proc is not None:
            try:
                proc.wait(timeout=_WAIT_TIMEOUT)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
            # Close the stdout/stderr pipe fds — Popen.__del__ would eventually,
            # but a flapping link rebuilds often enough to march toward EMFILE
            # before GC runs. stdin is already closed above.
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()
        # Break the Thread→bound-method→self reference cycle so this disposable
        # transport refcounts away promptly instead of waiting for the cyclic GC.
        self._reader = self._writer = self._err = None


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
