#!/usr/bin/env python3
"""Implements a CAN extension for the Zelos App"""

import logging
import signal
from pathlib import Path
from types import FrameType

import zelos_sdk
from zelos_sdk.extensions import load_config
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_can.codec import CanCodec

DEMO_DBC_PATH = Path(__file__).parent / "zelos_extension_can" / "demo" / "demo.dbc"

# Configure basic logging before SDK initialization
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize SDK (required before adding trace logging handler)
zelos_sdk.init(name="can", actions=True)

# Add the built-in handler to capture logs at INFO level and above
# (DEBUG logs won't be sent to backend to avoid duplicate trace data)
handler = TraceLoggingHandler("can_log")
handler.setLevel(logging.INFO)

# Add trace logging handler to send logs to Zelos
handler = TraceLoggingHandler("zelos_extension_can_logger")
logging.getLogger().addHandler(handler)

# Load configuration from config.json
config = load_config()

# If demo mode is selected, override with demo settings
if config.get("interface") == "demo":
    logger.info("Demo mode enabled via config")
    config = {
        "interface": "virtual",  # Use virtual CAN bus for demo
        "channel": "vcan0",
        "dbc_file": str(DEMO_DBC_PATH),
        "demo_mode": True,  # Flag for codec to enable demo simulation
    }
    logger.info("Demo mode: using built-in EV simulator")

# Create CAN codec
codec = CanCodec(config)

# Register interactive actions
zelos_sdk.actions_registry.register(codec)


def shutdown_handler(signum: int, frame: FrameType | None) -> None:
    """Handle graceful shutdown on SIGTERM or SIGINT.

    :param signum: Signal number (SIGTERM=15, SIGINT=2)
    :param frame: Current stack frame
    """
    logger.info("Shutting down...")
    codec.stop()


# Register signal handlers for graceful shutdown
signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

# Run
if __name__ == "__main__":
    logger.info("Starting zelos-extension-can")
    codec.start()
    codec.run()
