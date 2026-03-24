"""Regression tests for CAN frame loss using python-can virtual interface.

Uses the virtual interface (cross-platform, no kernel deps) to verify that
the codec receive queue correctly decouples recv from processing.  Any frame
loss in these tests points to GIL starvation in the processing path.
"""

import threading
import time
from pathlib import Path
from unittest.mock import patch

import can
import zelos_sdk

from zelos_extension_can.codec import CanCodec

TEST_DBC = Path(__file__).parent / "files" / "test.dbc"
VIRTUAL_CHANNEL = "test_frame_loss"
FRAME_COUNT = 2000
DRAIN_TIMEOUT = 30


def _make_codec(
    with_tracewriter: bool, trz_path: Path | None = None
) -> tuple[CanCodec, object | None]:
    config = {
        "interface": "virtual",
        "channel": VIRTUAL_CHANNEL,
        "database_file": str(TEST_DBC),
        "log_raw_frames": True,
        "emit_schemas_on_init": False,
    }
    writer = None
    if with_tracewriter and trz_path:
        writer = zelos_sdk.TraceWriter(str(trz_path))
        writer.__enter__()
    codec = CanCodec(config)
    return codec, writer


def _send_frames(count: int) -> float:
    bus = can.Bus(interface="virtual", channel=VIRTUAL_CHANNEL)
    msg = can.Message(
        arbitration_id=0x64,
        data=bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08]),
        is_extended_id=False,
    )
    t0 = time.monotonic()
    for _ in range(count):
        bus.send(msg)
    bus.shutdown()
    return time.monotonic() - t0


def _wait_drain(codec: CanCodec, expected: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if codec.metrics.messages_received >= expected and codec._rx_queue.empty():
            time.sleep(0.05)
            if codec._rx_queue.empty():
                return True
        time.sleep(0.01)
    return False


def test_no_loss_without_tracewriter():
    """Baseline: without TraceWriter, zero frames should be lost."""
    codec, _ = _make_codec(with_tracewriter=False)
    codec.start()
    notifier = can.Notifier(codec.bus, [codec])

    try:
        sender = threading.Thread(target=_send_frames, args=(FRAME_COUNT,))
        sender.start()
        sender.join(timeout=30)

        assert _wait_drain(codec, FRAME_COUNT, DRAIN_TIMEOUT), (
            f"Timed out: received {codec.metrics.messages_received}/{FRAME_COUNT}"
        )
        assert codec.metrics.messages_received == FRAME_COUNT
        assert codec.metrics.rx_queue_drops == 0
    finally:
        notifier.stop()
        codec.stop()


def test_no_loss_with_tracewriter(tmp_path):
    """With TraceWriter active, zero frames should be lost.

    NOTE: This test is expected to FAIL before the SDK GIL fix is applied.
    Once the SDK releases GIL during log_at()/log(), this should pass.
    """
    trz_path = tmp_path / "test.trz"
    codec, writer = _make_codec(with_tracewriter=True, trz_path=trz_path)
    codec.start()
    notifier = can.Notifier(codec.bus, [codec])

    try:
        sender = threading.Thread(target=_send_frames, args=(FRAME_COUNT,))
        sender.start()
        sender.join(timeout=30)

        assert _wait_drain(codec, FRAME_COUNT, DRAIN_TIMEOUT), (
            f"Timed out: received {codec.metrics.messages_received}/{FRAME_COUNT}"
        )
        assert codec.metrics.messages_received == FRAME_COUNT
        assert codec.metrics.rx_queue_drops == 0
    finally:
        notifier.stop()
        codec.stop()
        if writer:
            time.sleep(0.2)
            writer.__exit__(None, None, None)


def test_rx_queue_drops_tracked():
    """Verify rx_queue_drops metric increments when queue is full."""
    config = {
        "interface": "virtual",
        "channel": VIRTUAL_CHANNEL + "_drops",
        "database_file": str(TEST_DBC),
        "log_raw_frames": False,
        "emit_schemas_on_init": False,
        "rx_queue_size": 1,
    }
    with patch("zelos_sdk.TraceSource"):
        codec = CanCodec(config)

    for _ in range(10):
        msg = can.Message(arbitration_id=0x64, data=b"\x00" * 8)
        codec.on_message_received(msg)

    assert codec.metrics.rx_queue_drops > 0, "Expected some drops with queue size 1"
