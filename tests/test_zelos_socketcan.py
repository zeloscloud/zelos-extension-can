"""Functional tests for the zelos-socketcan interface in the extension.

Two layers:

  * Platform guard (always runs): selecting ``zelos-socketcan`` on a
    non-Linux host must fail fast with a clear, actionable error rather than
    python-can's opaque "interface not found".

  * End-to-end over vcan (opt-in via ``ZELOS_CAN_TEST_SOCKETCAN=1``): a codec
    configured with ``interface="zelos-socketcan"`` decodes received frames
    and transmits via the agent ``send_message`` / periodic actions, proving
    the Rust-backed bus is a drop-in for the extension's tracing + transmit
    paths.
"""

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import can
import pytest

from zelos_extension_can.codec import CanCodec

TEST_DBC = str(Path(__file__).parent / "files" / "test.dbc")
WIRE_ID = 100  # DUT_Status (8 bytes, standard id) in tests/files/test.dbc


# ── Platform guard (always runs) ────────────────────────────────────────────


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_zelos_socketcan_rejected_off_linux(platform):
    config = {
        "interface": "zelos-socketcan",
        "channel": "can0",
        "database_file": TEST_DBC,
    }
    with patch("zelos_sdk.TraceSource"):
        codec = CanCodec(config)
    with (
        patch("zelos_extension_can.codec.sys.platform", platform),
        pytest.raises(can.CanInterfaceNotImplementedError, match="Linux-only"),
    ):
        codec.start()


# ── End-to-end over vcan (opt-in) ───────────────────────────────────────────


def _vcan_available() -> bool:
    if os.environ.get("ZELOS_CAN_TEST_SOCKETCAN") != "1" or sys.platform != "linux":
        return False
    if importlib.util.find_spec("zelos_can") is None:
        return False
    iface = os.environ.get("ZELOS_CAN_TEST_IFACE", "vcan0")
    try:
        result = subprocess.run(
            ["ip", "link", "show", iface], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and "UP" in result.stdout


vcan_only = pytest.mark.skipif(
    not _vcan_available(),
    reason="needs ZELOS_CAN_TEST_SOCKETCAN=1, Linux, zelos_can, and an up vcan iface",
)
IFACE = os.environ.get("ZELOS_CAN_TEST_IFACE", "vcan0")


@pytest.fixture
def zelos_codec():
    """A started CanCodec on the zelos-socketcan interface.

    The native path runs the full Rust pipeline (recv -> decode -> trace) and
    requires a real zelos_sdk.TraceSource, so this uses a live SDK source (no
    TraceWriter is attached; we assert on the codec's native metrics). A unique
    bus_name isolates the trace source per test run.
    """
    config = {
        "interface": "zelos-socketcan",
        "channel": IFACE,
        "database_file": TEST_DBC,
        "receive_own_messages": False,
    }
    codec = CanCodec(config, bus_name=f"zsc_{int(time.time() * 1000) % 100000}")
    codec.start()
    yield codec
    codec.stop()


def _rx_metrics(codec):
    return codec.get_tx_state()["bus"]["metrics"]


@vcan_only
def test_zelos_socketcan_decodes_received_frames(zelos_codec):
    """The native Rust pipeline receives and decodes frames off the wire —
    no python-can Notifier, no cantools; RX counters come from the Rust codec
    and surface through get_tx_state."""
    sender = can.Bus(interface="socketcan", channel=IFACE)
    try:
        for _ in range(3):
            sender.send(can.Message(arbitration_id=WIRE_ID, data=bytes(8), is_extended_id=False))
        deadline = time.time() + 2.0
        while _rx_metrics(zelos_codec)["messages_received"] == 0 and time.time() < deadline:
            time.sleep(0.02)
        m = _rx_metrics(zelos_codec)
        assert m["messages_received"] >= 1, m
        assert m["messages_decoded"] >= 1, m
    finally:
        sender.shutdown()


@vcan_only
def test_zelos_socketcan_send_raw_action(zelos_codec):
    """The send_raw agent action transmits on the zelos-socketcan bus."""
    observer = can.Bus(interface="socketcan", channel=IFACE)
    try:
        result = zelos_codec.send_raw(f"0x{WIRE_ID:x}", "01 02 03 04 05 06 07 08")
        assert result["can_id"] == WIRE_ID, result
        got = observer.recv(timeout=1.0)
        assert got is not None
        assert got.arbitration_id == WIRE_ID
        assert bytes(got.data) == bytes([1, 2, 3, 4, 5, 6, 7, 8])
    finally:
        observer.shutdown()
