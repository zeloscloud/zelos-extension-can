#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "python-can",
#     "cantools",
#     "zelos-sdk",
# ]
# ///

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

# Add parent directory to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from zelos_extension_can.codec import CanCodec
from zelos_extension_can.demo.demo import run_demo_ev_simulation

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    """Run SocketCAN example with EV simulation."""
    # Path to demo DBC file
    demo_dbc = Path(__file__).parent.parent / "demo" / "demo.dbc"

    # Configuration for SocketCAN
    config = {
        "interface": "socketcan",
        "channel": "vcan0",  # Change to 'can0' for real hardware
        "database_file": str(demo_dbc),
        "log_raw_frames": False,  # Set True to see raw CAN frames
        "emit_schemas_on_init": False,  # Lazy schema generation
        "timestamp_mode": "ignore",  # Use system time for trace events
    }

    logger.info("=" * 80)
    logger.info("SocketCAN Example - EV Simulation")
    logger.info("=" * 80)
    logger.info("Interface: %s", config["interface"])
    logger.info("Channel: %s", config["channel"])
    logger.info("Database: %s", demo_dbc.name)
    logger.info("=" * 80)

    try:
        # Create and start CAN codec
        codec = CanCodec(config)
        codec.start()

        logger.info("CAN bus started successfully")
        logger.info("Starting EV simulation (press Ctrl+C to stop)...")

        # Start EV simulation task
        sim_task = asyncio.create_task(run_demo_ev_simulation(codec.bus, codec.db, codec))

        # Periodically print metrics
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
