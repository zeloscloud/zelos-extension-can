#!/usr/bin/env python3
"""Zelos App extension for CAN"""

import logging
import signal
import sys
from types import FrameType

import zelos_sdk
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_can.extension import SensorMonitor
from zelos_extension_can.utils.config import load_config

# Configure logging before adding SDK handlers so DEBUG-level logs are emitted
logging.basicConfig(level=logging.DEBUG)

# Initialize SDK
zelos_sdk.init(name="zelos_extension_can", actions=True)

# Add the built-in handler to capture all logs
handler = TraceLoggingHandler("zelos_extension_can_logger")
logging.getLogger().addHandler(handler)
logger = logging.getLogger(__name__)

# Load configuration
config = load_config()

# Create sensor monitor
monitor = SensorMonitor(config)

# Register interactive actions
zelos_sdk.actions_registry.register(monitor)


def shutdown_handler(signum: int, frame: FrameType | None) -> None:
    """Handle graceful shutdown.

    :param signum: Signal number
    :param frame: Current stack frame
    """
    logger.info("Shutting down...")
    monitor.stop()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

# Run
if __name__ == "__main__":
    logger.info("Starting zelos-extension-can")
    monitor.start()
    monitor.run()
