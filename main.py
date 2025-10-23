#!/usr/bin/env python3
"""Zelos CAN extension - CAN bus monitoring and DBC decoding."""

import argparse
import logging
import signal
import sys
from pathlib import Path
from types import FrameType

import zelos_sdk
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_can.codec import CanCodec
from zelos_extension_can.utils.config import load_config, validate_config

DEMO_DBC_PATH = Path(__file__).parent / "zelos_extension_can" / "demo" / "demo.dbc"

# Configure logging before adding SDK handlers so DEBUG-level logs are emitted
logging.basicConfig(level=logging.INFO)

# Initialize SDK
zelos_sdk.init(name="can", actions=True)

# Add the built-in handler to capture all logs
handler = TraceLoggingHandler("can_log")
logging.getLogger().addHandler(handler)
logger = logging.getLogger(__name__)

# Parse command line arguments
parser = argparse.ArgumentParser(description="Zelos CAN Extension")
parser.add_argument(
    "--demo",
    action="store_true",
    help="Run in demo mode with built-in EV simulator",
)
args = parser.parse_args()

# Load and validate configuration
config = load_config()

# Override with demo mode if requested via CLI flag
if args.demo:
    logger.info("Demo mode enabled via --demo flag")
    config["interface"] = "demo"

# Handle demo interface selection
if config.get("interface") == "demo":
    logger.info("Demo mode: using built-in EV simulator")
    config["demo_mode"] = True
    config["interface"] = "virtual"
    config["channel"] = "vcan0"
    config["dbc_file"] = str(DEMO_DBC_PATH)

# Handle "other" interface - merge config_json into main config
if config.get("interface") == "other":
    logger.info("Using custom interface from config_json")
    import json

    if "config_json" not in config or not config["config_json"]:
        logger.error("'other' interface requires config_json with interface and channel")
        sys.exit(1)
    try:
        custom_config = json.loads(config["config_json"])
        if "interface" not in custom_config:
            logger.error("config_json must include 'interface' key")
            sys.exit(1)
        if "channel" not in custom_config:
            logger.error("config_json must include 'channel' key")
            sys.exit(1)
        # Merge custom config into main config
        config["interface"] = custom_config.pop("interface")
        config["channel"] = custom_config.pop("channel")
        # Update config_json with remaining custom parameters
        config["config_json"] = json.dumps(custom_config) if custom_config else ""
        logger.info(f"Custom interface: {config['interface']}, channel: {config['channel']}")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config_json: {e}")
        sys.exit(1)

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
