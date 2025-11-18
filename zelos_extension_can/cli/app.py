"""App-based configuration mode for CAN tracing."""

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import zelos_sdk
from zelos_sdk.extensions import load_config

from ..codec import CanCodec
from .utils import setup_shutdown_handler

logger = logging.getLogger(__name__)


def run_app_mode(demo: bool, file: Path | None, demo_dbc_path: Path) -> None:
    """Run CAN extension in app-based configuration mode.

    :param demo: Enable demo mode
    :param file: Optional output file for trace recording
    :param demo_dbc_path: Path to demo DBC file
    """
    # Load and validate configuration
    config = load_config()

    # Apply log level from config
    log_level_str = config.get("log_level", "INFO")
    try:
        log_level = getattr(logging, log_level_str)
        logging.getLogger().setLevel(log_level)
        logger.info(f"Log level set to: {log_level_str}")
    except AttributeError:
        logger.warning(f"Invalid log level '{log_level_str}', using INFO")
        logging.getLogger().setLevel(logging.INFO)

    # Override with demo mode if requested via CLI flag
    if demo:
        logger.info("Demo mode enabled via --demo flag")
        config["interface"] = "demo"

    # Handle demo interface selection
    if config.get("interface") == "demo":
        logger.info("Demo mode: using built-in EV simulator")
        config["demo_mode"] = True
        config["interface"] = "virtual"
        config["channel"] = "vcan0"
        config["database_file"] = str(demo_dbc_path)
        config["receive_own_messages"] = (
            True  # Required for demo mode to receive simulated messages
        )
        config["log_raw_frames"] = True  # Enable raw CAN frame logging in demo mode

    # Handle "other" interface - merge config_json into main config
    if config.get("interface") == "other":
        logger.info("Using custom interface from config_json")

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

    # Determine output file if --file was specified
    output_file = None
    if file is not None:
        # If --file was given without a value, use UTC timestamp
        if str(file) == ".":
            utc_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            file = Path(f"{utc_timestamp}.trz")
        output_file = file
        logger.info(f"Recording trace to: {output_file}")

    # Create CAN codec
    codec = CanCodec(config)

    # Register interactive actions
    zelos_sdk.actions_registry.register(codec, "can_codec")

    # Initialize SDK
    zelos_sdk.init(name="can", log_level="info", actions=True)

    setup_shutdown_handler(codec)

    # Run with optional trace writer
    logger.info("Starting CAN extension")
    if output_file:
        with zelos_sdk.TraceWriter(str(output_file)):
            codec.start()
            codec.run()
    else:
        codec.start()
        codec.run()
