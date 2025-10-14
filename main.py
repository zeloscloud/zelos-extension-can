#!/usr/bin/env python3
"""Zelos CAN extension - CAN bus monitoring and DBC decoding."""

import logging
import signal
import sys
from types import FrameType

import zelos_sdk
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_can.can_codec import CanCodec
from zelos_extension_can.utils.config import load_config, validate_config

# Configure logging before adding SDK handlers so DEBUG-level logs are emitted
logging.basicConfig(level=logging.INFO)

# Initialize SDK
zelos_sdk.init(name="zelos_extension_can", actions=True)

# Add the built-in handler to capture all logs
handler = TraceLoggingHandler("zelos_extension_can_logger")
logging.getLogger().addHandler(handler)
logger = logging.getLogger(__name__)

# Load and validate configuration
config = load_config()
if errors := validate_config(config):
    logger.error("Configuration validation failed:")
    for error in errors:
        logger.error(f"  - {error}")
    sys.exit(1)

# Create CAN codec
codec = CanCodec(config)

# Register interactive actions
zelos_sdk.actions_registry.register(codec)


def shutdown_handler(signum: int, frame: FrameType | None) -> None:
    """Handle graceful shutdown.

    :param signum: Signal number
    :param frame: Current stack frame
    """
    logger.info("Shutting down CAN extension...")
    codec.stop()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

# Run
if __name__ == "__main__":
    logger.info("Starting CAN extension")
    codec.start()
    codec.run()
