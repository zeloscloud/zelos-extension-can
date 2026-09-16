"""Runs the single-session remote shell under every POSIX shell on this box.

``SshTransport._remote_command`` is the one piece of this transport that executes
on the EDGE, under whatever ``/bin/sh`` it happens to have (dash, busybox ash,
bash). It is a string, so nothing else can check it: these tests run it for real
with stand-in ``candump``/``cansend`` on PATH and assert the three death paths
that keep a dead link from looking healthy — plus that TX lines actually reach
``cansend`` now that both directions share one session.

``kill 0`` signals the shell's whole process group, so every shell here is
started with ``start_new_session=True`` — which is how sshd runs a remote command
and the reason this file cannot take the test runner down with it.
"""

import contextlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from zelos_extension_can.ssh_socketcan import SshTransport

IFACE = "can0"
CMD = SshTransport._remote_command(IFACE)
# busybox ash is not installable on darwin; dash is the same ash lineage and is
# the /bin/sh of the Debian-family images these edges usually run.
SHELLS = [p for p in ("/bin/sh", "/bin/dash", "/bin/bash") if Path(p).exists()]


def _wait_until(pred, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not pred():
        time.sleep(0.02)
    return pred()


def _alive(pid: int) -> bool:
    """Is OUR candump stand-in still running? Matches on the command too, so a
    recycled pid can never read as alive (and nothing here ever signals a pid)."""
    ps = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    )
    return "sleep" in ps.stdout


class _Edge:
    """A fake edge: candump/cansend stand-ins on PATH plus their receipts."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.pid_file = root / "candump.pid"
        self.tx_log = root / "tx.log"
        self.proc: subprocess.Popen | None = None
        # cansend appends every frame it is handed.
        self._script("cansend", f'echo "$@" >> {self.tx_log}\n')

    def _script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def start(self, shell: str, *, candump_lives: bool) -> subprocess.Popen:
        """Spawn the remote shell. candump records its pid, then either sleeps
        (a live capture) or exits (death path 2)."""
        tail = "exec sleep 5\n" if candump_lives else "exit 3\n"
        self._script("candump", f"echo $$ > {self.pid_file}\n" + tail)
        self.proc = subprocess.Popen(
            [shell, "-c", CMD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.root,
            env={"PATH": f"{self.bin}:/usr/bin:/bin"},
            start_new_session=True,  # `kill 0` must never reach pytest
        )
        return self.proc

    def candump_pid(self) -> int:
        assert _wait_until(self.pid_file.exists), f"candump stand-in never ran: {self.diag()}"
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
        """Kill the shell's session, and ONLY while the shell is alive — once it
        has been reaped its pid is free to be recycled onto anything, and no test
        may fire SIGKILL at a pid it no longer owns. A stand-in leaked by a failed
        assertion expires on its own (``sleep 5``)."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(self.proc.pid, 9)  # start_new_session → pid is the pgid
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
    our stdin (local teardown) ends the read-loop, which kills candump, which
    ends the session — nothing orphaned on the edge."""
    proc = edge.start(shell, candump_lives=True)
    pid = edge.candump_pid()

    proc.stdin.write(b"123#AABB\n456#01\n")
    proc.stdin.flush()
    assert _wait_until(lambda: edge.tx_lines() == ["123#AABB", "456#01"])

    proc.stdin.close()
    assert _wait_until(lambda: proc.poll() is not None), (
        f"session outlived its stdin: {edge.diag()}"
    )
    assert _wait_until(lambda: not _alive(pid)), f"candump orphaned on the edge: {edge.diag()}"


@pytest.mark.parametrize("shell", SHELLS)
def test_candump_death_ends_the_session(shell, edge):
    """Death path 2: candump exiting (crash, iface down) must end the whole
    session so the local reader sees EOF instead of a silent RX starve."""
    proc = edge.start(shell, candump_lives=False)
    assert _wait_until(lambda: proc.poll() is not None), f"session survived candump: {edge.diag()}"


@pytest.mark.parametrize("shell", SHELLS)
def test_signalled_shell_reaps_candump(shell, edge):
    """Death path 3: the wrapper shell killed from outside still reaps candump —
    dash and busybox ash skip the EXIT trap on an untrapped fatal signal, which
    is why the trap also covers TERM/INT/HUP."""
    proc = edge.start(shell, candump_lives=True)
    pid = edge.candump_pid()

    proc.terminate()

    assert _wait_until(lambda: proc.poll() is not None)
    assert _wait_until(lambda: not _alive(pid)), f"candump orphaned on the edge: {edge.diag()}"
