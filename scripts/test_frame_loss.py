#!/usr/bin/env python3
"""Cross-platform frame-loss test for CAN codec.

Uses python-can's 'virtual' interface (in-process software queue) by default,
so it works identically on Linux and macOS with no kernel/hardware deps.
This isolates GIL starvation specifically — the virtual bus never drops frames
at the transport layer, so any loss proves GIL contention in the codec/SDK.

On Linux with vcan0, pass --interface socketcan --channel vcan0 to also test
the kernel SocketCAN buffer path (requires canplayer for frame injection).

Usage:
  uv run python scripts/test_frame_loss.py --with-tracewriter
  uv run python scripts/test_frame_loss.py --without-tracewriter
  uv run python scripts/test_frame_loss.py --frames 10000 --with-tracewriter
  uv run python scripts/test_frame_loss.py --interface socketcan --channel vcan0 --with-tracewriter
"""

import argparse
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path

import can
import zelos_sdk

from zelos_extension_can.codec import CanCodec

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("frame_loss_test")
log.setLevel(logging.INFO)

TEST_DBC = Path(__file__).resolve().parent.parent / "tests" / "files" / "test.dbc"
DRAIN_TIMEOUT = 30


def send_frames(bus: can.BusABC, count: int) -> float:
    """Send count CAN frames as fast as possible. Returns elapsed seconds."""
    msg = can.Message(
        arbitration_id=0x64,
        data=bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08]),
        is_extended_id=False,
    )
    t0 = time.monotonic()
    for _ in range(count):
        bus.send(msg)
    return time.monotonic() - t0


def wait_drain(codec: CanCodec, expected: int, timeout_s: float) -> bool:
    """Wait for codec to process all expected messages."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if codec.metrics.messages_received >= expected and codec._rx_queue.empty():
            time.sleep(0.1)
            if codec._rx_queue.empty():
                return True
        time.sleep(0.02)
    return False


def run_test(
    interface: str,
    channel: str,
    n_frames: int,
    with_tracewriter: bool,
    verbose: bool,
) -> int:
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not TEST_DBC.exists():
        log.error("Test DBC not found: %s", TEST_DBC)
        return 1

    trz_dir = Path(tempfile.mkdtemp())
    trz_path = trz_dir / "frame_loss_test.trz"

    config = {
        "interface": interface,
        "channel": channel,
        "database_file": str(TEST_DBC),
        "log_raw_frames": True,
        "emit_schemas_on_init": False,
    }

    writer = None
    if with_tracewriter:
        writer = zelos_sdk.TraceWriter(str(trz_path))
        writer.__enter__()

    codec = CanCodec(config)
    codec.start()
    notifier = can.Notifier(codec.bus, [codec])

    sender_bus = can.Bus(interface=interface, channel=channel)

    # Send frames on a background thread to avoid blocking the main thread
    log.info(
        "Sending %d frames via %s/%s (tracewriter=%s)",
        n_frames,
        interface,
        channel,
        with_tracewriter,
    )
    t_send = [0.0]

    def _send():
        t_send[0] = send_frames(sender_bus, n_frames)

    sender_thread = threading.Thread(target=_send)
    sender_thread.start()
    sender_thread.join(timeout=60)
    sender_bus.shutdown()

    send_elapsed = t_send[0]
    rate = n_frames / max(send_elapsed, 0.001)
    log.info("Send complete in %.2fs (%.0f frames/s)", send_elapsed, rate)

    # Drain
    log.info("Waiting for codec to drain (timeout %ds)...", DRAIN_TIMEOUT)
    drained = wait_drain(codec, n_frames, DRAIN_TIMEOUT)
    log.info("Drain complete (success=%s, qsize=%d)", drained, codec._rx_queue.qsize())

    # Stop
    notifier.stop()
    codec.stop()

    if writer:
        time.sleep(0.3)
        writer.__exit__(None, None, None)

    # Cleanup temp files
    trz_path.unlink(missing_ok=True)
    trz_dir.rmdir()

    # Results
    m = codec.metrics
    received = m.messages_received
    lost = n_frames - received

    print()
    print("=" * 62)
    print("  FRAME LOSS TEST RESULTS")
    print("=" * 62)
    print(f"  Interface:                     {interface}/{channel}")
    print(f"  TraceWriter active:            {'YES' if with_tracewriter else 'NO'}")
    print(f"  Frames sent:                   {n_frames:>10,}")
    print(f"  codec.messages_received:       {received:>10,}")
    print(f"  codec.messages_decoded:        {m.messages_decoded:>10,}")
    print(f"  codec.decode_errors:           {m.decode_errors:>10,}")
    print(f"  codec.unknown_messages:        {m.unknown_messages:>10,}")
    print(f"  rx_queue_drops:                {m.rx_queue_drops:>10,}")
    print("  ---")
    print(f"  Frames lost:                   {lost:>+10,}")
    pct = (lost / n_frames * 100) if n_frames else 0
    print(f"  Loss rate:                     {pct:>9.2f}%")
    print(f"  Effective send rate:           {rate:>9.0f} frames/s")
    print("=" * 62)

    if lost == 0 and m.rx_queue_drops == 0:
        print("  PASS: Zero frame loss.")
        return 0
    else:
        print(f"  FAIL: {lost:,} frames lost ({pct:.2f}%).")
        return 2


def main():
    p = argparse.ArgumentParser(description="Cross-platform CAN frame loss test")
    p.add_argument("--interface", default="virtual", help="python-can interface (default: virtual)")
    p.add_argument("--channel", default="test", help="CAN channel (default: test)")
    p.add_argument(
        "--frames", type=int, default=5000, help="Number of frames to send (default: 5000)"
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--with-tracewriter",
        action="store_true",
        help="Run with TraceWriter active (tests GIL contention)",
    )
    mode.add_argument(
        "--without-tracewriter",
        action="store_true",
        help="Run without TraceWriter (baseline, should always pass)",
    )
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    return run_test(args.interface, args.channel, args.frames, args.with_tracewriter, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
