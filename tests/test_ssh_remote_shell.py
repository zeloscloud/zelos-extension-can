"""Runs the single-session remote shell under every POSIX shell on this box.

``SshTransport._remote_command`` is a STRING that executes on the EDGE under
whatever ``/bin/sh`` it has, so nothing but execution can check it. Each test
runs it for real with stand-in ``candump``/``cansend`` on PATH and asserts, for
every death path, that our stdout reaches EOF (the local reader's only signal)
and that nothing is orphaned on the edge — plus that TX lines reach ``cansend``
now that both directions share one session.

``kill 0`` signals the shell's whole process group, so every shell here is
started with ``start_new_session=True`` — which is how sshd runs a remote command
and the reason this file cannot take the test runner down with it. The one case
that must NOT get a session for free (a daemon-hosted sshd hands its own group to
every session) gets a stand-in caller group instead, never pytest's.
"""

import contextlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import wait_until

from zelos_extension_can.ssh_socketcan import SshTransport

IFACE = "can0"
CMD = SshTransport._remote_command(IFACE)
# busybox ash is not installable on darwin; dash is the same ash lineage and is
# the /bin/sh of the Debian-family images these edges usually run.
SHELLS = [p for p in ("/bin/sh", "/bin/dash", "/bin/bash") if Path(p).exists()]
# darwin has no setsid, so the own-group case brings its own: util-linux and
# busybox both exec in place when the caller does not lead its group, which is
# the only case the remote command calls it in.
SETSID_STANDIN = """\
import os, sys

if os.getpgrp() == os.getpid():
    if os.fork():  # a group leader cannot setsid, so real setsid forks and exits
        os._exit(0)
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])
"""


def _pgid(pid: int) -> int:
    """The process group of ``pid``; raises if it is gone (no /proc on darwin)."""
    ps = subprocess.run(
        ["ps", "-o", "pgid=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return int(ps.stdout.strip())  # never a default: callers signal what this returns


def _alive(pid: int) -> bool:
    """Is OUR candump stand-in still running? Matches on the command too, so a
    recycled pid can never read as alive (and nothing here ever signals a pid)."""
    ps = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    )
    return "sleep" in ps.stdout


def _stdout_eof(proc: subprocess.Popen) -> bool:
    """Did the channel's stdout close? The local reader treats EOF, not the
    wrapper's exit status, as "the link is gone" — so every death path must
    reach it (i.e. no remote process is still holding the fd)."""
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)

    def closed() -> bool:
        try:
            return os.read(fd, 4096) == b""
        except BlockingIOError:
            return False  # still open, nothing to read
        except OSError:
            return True

    return wait_until(closed)


class _Edge:
    """A fake edge: candump/cansend stand-ins on PATH plus their receipts."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.pid_file = root / "candump.pid"
        self.sibling_file = root / "sibling.pid"
        self.tx_log = root / "tx.log"
        self.proc: subprocess.Popen | None = None
        # cansend appends every frame it is handed.
        self._script("cansend", f'echo "$@" >> {self.tx_log}\n')

    def _script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def _candump(self, lives: bool) -> None:
        """candump records its pid, then either sleeps (a live capture) or exits
        (death path 2). The sleep outlasts every assertion here, so a reap that
        never happened cannot pass by timing."""
        tail = "exec sleep 30\n" if lives else "exit 3\n"
        self._script("candump", f"echo $$ > {self.pid_file}\n" + tail)

    def _spawn(self, argv: list[str]) -> subprocess.Popen:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.root,
            env={"PATH": f"{self.bin}:/usr/bin:/bin"},
            start_new_session=True,  # `kill 0` must never reach pytest
        )
        return self.proc

    def start(self, shell: str, *, candump_lives: bool) -> subprocess.Popen:
        """Spawn the remote shell the way OpenSSH's sshd does: its own session."""
        self._candump(candump_lives)
        return self._spawn([shell, "-c", CMD])

    def start_in_caller_group(self, shell: str) -> subprocess.Popen:
        """Spawn the remote shell the way a daemon-hosted sshd does: sharing the
        CALLER's process group, which already holds an unrelated sibling.

        That is the Tailscale SSH shape (`tailscaled be-child ssh --cmd=...`),
        where every session lands in the daemon's group. The caller group is a
        session of its own, so a regression reaps the sibling, never pytest.
        """
        self._candump(True)
        setsid_py = self.root / "setsid.py"
        setsid_py.write_text(SETSID_STANDIN)
        interp = f"{shlex.quote(sys.executable)} {shlex.quote(str(setsid_py))}"
        self._script("setsid", f'exec {interp} "$@"\n')
        caller = self.root / "caller.sh"
        caller.write_text(
            "#!/bin/sh\n"
            # The sibling joins this group first (no job control, so it does) and
            # drops the stdio it inherited, which the session's EOF is measured on.
            f"sleep 30 >/dev/null 2>&1 &\necho $! > {self.sibling_file}\n"
            '"$1" -c "$2"\n'
            "exit $?\n"  # never the last command: the session must be a CHILD of ours
        )
        caller.chmod(0o755)
        return self._spawn([str(caller), shell, CMD])

    def sibling_pid(self) -> int:
        assert wait_until(self.sibling_file.exists), f"caller never started: {self.diag()}"
        return int(self.sibling_file.read_text().strip())

    def candump_pid(self) -> int:
        assert wait_until(self.pid_file.exists), f"candump stand-in never ran: {self.diag()}"
        return int(self.pid_file.read_text().strip())

    def diag(self) -> str:
        """Why the remote shell misbehaved: its exit code and whatever it said."""
        assert self.proc is not None
        out = []
        for name in ("stdout", "stderr"):
            stream = getattr(self.proc, name)
            fd = stream.fileno()
            os.set_blocking(fd, False)
            with contextlib.suppress(OSError):
                out.append(f"{name}={os.read(fd, 4096)!r}")
        return f"rc={self.proc.poll()} " + " ".join(out)

    def tx_lines(self) -> list[str]:
        """The frames cansend was handed, iface column dropped."""
        if not self.tx_log.exists():
            return []
        return [ln.split()[-1] for ln in self.tx_log.read_text().splitlines() if ln.strip()]

    def cleanup(self) -> None:
        """Kill the process groups the stand-ins live in — the one we spawned,
        plus candump's own, which is a DIFFERENT group once the session moves
        itself out of the caller's. Safe after the shell is reaped: a
        process-group id is not reused while the group still has a member."""
        if self.proc is None:
            return
        groups = {self.proc.pid}  # start_new_session → pid is the pgid
        with contextlib.suppress(Exception):
            groups.add(_pgid(int(self.pid_file.read_text().strip())))
        for pgid in groups:
            if pgid > 1:  # never 0 (pytest's own group) or -1 (every process)
                with contextlib.suppress(Exception):
                    os.killpg(pgid, 9)
        with contextlib.suppress(Exception):
            self.proc.wait(timeout=2)


@pytest.fixture
def edge(tmp_path):
    e = _Edge(tmp_path)
    yield e
    e.cleanup()


@pytest.mark.parametrize("shell", SHELLS)
def test_tx_flows_then_stdin_close_reaps_candump(shell, edge):
    """Death path 1: TX lines reach cansend on the shared session, and closing
    our stdin (local teardown) ends the read loop, whose EXIT trap reaps
    candump — nothing orphaned on the edge, stdout closed."""
    proc = edge.start(shell, candump_lives=True)
    pid = edge.candump_pid()

    proc.stdin.write(b"123#AABB\n456#01\n")
    proc.stdin.flush()
    assert wait_until(lambda: edge.tx_lines() == ["123#AABB", "456#01"])

    proc.stdin.close()
    assert wait_until(lambda: proc.poll() is not None), f"session outlived its stdin: {edge.diag()}"
    assert wait_until(lambda: not _alive(pid)), f"candump orphaned on the edge: {edge.diag()}"
    assert _stdout_eof(proc), "stdout still held open after the session ended"


@pytest.mark.parametrize("shell", SHELLS)
def test_candump_death_ends_the_session(shell, edge):
    """Death path 2: candump exiting (crash, iface down) signals the shell, which
    must end the session so the local reader sees EOF, not a silent RX starve."""
    proc = edge.start(shell, candump_lives=False)
    assert wait_until(lambda: proc.poll() is not None), f"session survived candump: {edge.diag()}"
    assert _stdout_eof(proc), "stdout still held open after candump died"


@pytest.mark.parametrize("shell", SHELLS)
def test_tx_loop_death_ends_the_session(shell, edge):
    """Death path 4: the TX side dying must not leave RX streaming on a session
    that silently drops every frame it is handed. The read loop is the shell's
    own body, so here cansend kills it mid-frame; the trap reaps candump."""
    edge._script("cansend", "kill -TERM $PPID\n")  # the TX loop blows up on a frame
    proc = edge.start(shell, candump_lives=True)
    pid = edge.candump_pid()

    proc.stdin.write(b"123#AABB\n")
    proc.stdin.flush()

    assert wait_until(lambda: proc.poll() is not None), (
        f"session survived its TX loop: {edge.diag()}"
    )
    assert wait_until(lambda: not _alive(pid)), f"candump orphaned on the edge: {edge.diag()}"
    assert _stdout_eof(proc), "RX kept streaming after the TX loop died"


@pytest.mark.parametrize("shell", SHELLS)
def test_signalled_shell_reaps_candump(shell, edge):
    """Death path 3: the wrapper shell killed from outside still reaps candump —
    dash and busybox ash skip the EXIT trap on an untrapped fatal signal, which
    is why the trap also covers TERM/INT/HUP."""
    proc = edge.start(shell, candump_lives=True)
    pid = edge.candump_pid()

    proc.terminate()

    assert wait_until(lambda: proc.poll() is not None)
    assert wait_until(lambda: not _alive(pid)), f"candump orphaned on the edge: {edge.diag()}"
    assert _stdout_eof(proc), "stdout still held open after the shell was signalled"


@pytest.mark.parametrize("shell", SHELLS)
def test_session_owns_its_process_group(shell, edge):
    """The field case: a session handed the caller's process group must move out
    of it before the trap is armed, or its `kill 0` reaps everything else in
    there — the other buses' candumps on the same edge, and the operator's own
    unrelated sessions. Death path 1 still has to reap OUR candump."""
    proc = edge.start_in_caller_group(shell)
    pid, sibling = edge.candump_pid(), edge.sibling_pid()
    caller_pgid = proc.pid  # start_new_session → the caller leads the group

    assert _pgid(sibling) == caller_pgid, "the sibling is not in the caller's group"
    assert _pgid(pid) != caller_pgid, f"session still in the caller's group: {edge.diag()}"

    proc.stdin.close()
    assert wait_until(lambda: proc.poll() is not None), f"session outlived its stdin: {edge.diag()}"
    assert wait_until(lambda: not _alive(pid)), f"candump orphaned on the edge: {edge.diag()}"
    assert _alive(sibling), "the session reaped a sibling it does not own"
    assert _stdout_eof(proc), "stdout still held open after the session ended"
