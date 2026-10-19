#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["zelos-can[python-can]>=0.0.11a10", "zelos-sdk"]
# ///
"""Trace a PCAN bus against the demo DBC to a .trz, sending 0x100 every 100 ms.

Needs the PCAN driver (PCANBasic). Set the channel and bitrate for your adapter and
bus. Ctrl+C stops.
"""

import contextlib
import logging
import time

import zelos_sdk
from zelos_can.bus.config import DEMO_DBC, prepare_bus_config
from zelos_can.codec import CanCodec

ENTRY = {
    "interface": "pcan",
    "channel": "PCAN_USBBUS1",
    "bitrate": 500000,
    "database_files": [str(DEMO_DBC)],
}


def main() -> None:
    codec = CanCodec(prepare_bus_config(ENTRY), bus_name="PCAN_USBBUS1")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    with zelos_sdk.TraceWriter(f"pcan-{stamp}.trz"):
        try:
            codec.start()
            task = codec.start_periodic_raw("0x100", "01 02", period_ms=100)["task_id"]
            with contextlib.suppress(KeyboardInterrupt):
                codec.run()  # decode and trace until Ctrl+C
            codec.stop_periodic(task)
            bus = codec.get_tx_state()["bus"]
            print(bus["status"], bus["metrics"])
        finally:
            codec.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
