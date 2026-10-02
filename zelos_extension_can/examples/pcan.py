#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     # Placeholder: needs the zelos-can release carrying src#1603.
#     "zelos-can[python-can]>=0.0.11a1",
#     "zelos-sdk",
# ]
# ///
"""Example: PCAN interface with EV simulation.

Usage:
    uv run pcan.py
    # or
    python pcan.py
"""

import asyncio
import contextlib
import logging
from pathlib import Path

from zelos_can import demo
from zelos_can.codec import CanCodec
from zelos_can.demo.demo import run_demo_ev_simulation

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def send_message_example(codec: CanCodec):
    """Example: Send a single CAN message."""
    logger.info("Example: Sending single Motor_Command message...")

    result = codec.send_message(
        msg_id=0x202,  # Motor_Command frame ID
        data="64 00 10 27 01 01",  # Example data
        extended_id=False,
    )
    logger.info("Send result: %s", result)


async def periodic_message_example(codec: CanCodec):
    """Example: Start periodic transmission of a message."""
    logger.info("Example: Starting periodic Gateway_VehicleSpeed transmission...")

    # Start periodic message at 100ms interval
    result = codec.start_periodic(
        msg_id=0x300,  # Gateway_VehicleSpeed frame ID
        period=0.1,  # 100ms
        data="00 00 00 00 00 00 18 00",  # Stationary vehicle
        extended_id=False,
    )
    logger.info("Periodic start result: %s", result)

    # Let it run for 5 seconds
    await asyncio.sleep(5)

    # Stop periodic transmission
    result = codec.stop_periodic(task_name="0x0300")
    logger.info("Periodic stop result: %s", result)


async def main():
    """Run PCAN example with EV simulation."""
    # Path to demo DBC file
    demo_dbc = Path(demo.__file__).parent / "demo.dbc"

    # Configuration for PCAN
    config = {
        "interface": "pcan",
        "channel": "PCAN_USBBUS1",  # Adjust for your device
        "bitrate": 500000,
        "database_files": [str(demo_dbc)],
        "log_raw_frames": False,  # Set True to see raw CAN frames
        "emit_schemas_on_init": True,  # Generate all schemas at startup
        "timestamp_mode": "auto",  # Auto-detect timestamp format
        "fd_mode": False,  # Set True for CAN-FD
    }

    logger.info("=" * 80)
    logger.info("PCAN Example - EV Simulation")
    logger.info("=" * 80)
    logger.info("Interface: %s", config["interface"])
    logger.info("Channel: %s", config["channel"])
    logger.info("Bitrate: %d", config["bitrate"])
    logger.info("Database: %s", demo_dbc.name)
    logger.info("=" * 80)

    sim_task = None

    try:
        # Create and start CAN codec
        codec = CanCodec(config)
        codec.start()

        logger.info("CAN bus started successfully")

        # Show initial status
        status = codec.get_status()
        logger.info("Bus status: %s", status)

        # Run action examples
        await send_message_example(codec)
        await asyncio.sleep(2)
        await periodic_message_example(codec)

        logger.info("Starting EV simulation (press Ctrl+C to stop)...")

        # Start EV simulation task
        sim_task = asyncio.create_task(run_demo_ev_simulation(codec.bus, codec.db, codec))

        # Periodically print metrics and list periodic tasks
        while True:
            await asyncio.sleep(10)
            metrics = codec.get_metrics()
            logger.info(
                "Metrics: RX=%d decoded=%d errors=%d unknown=%d rate=%.1f msg/s",
                metrics["messages_received"],
                metrics["messages_decoded"],
                metrics["decode_errors"],
                metrics["unknown_messages"],
                metrics["messages_per_second"],
            )

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.exception("Error: %s", e)
    finally:
        if sim_task:
            sim_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sim_task
        codec.stop()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
