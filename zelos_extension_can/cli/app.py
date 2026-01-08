"""App-based configuration mode for CAN tracing."""

import asyncio
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


def _prepare_bus_config(bus_config: dict, demo_dbc_path: Path) -> dict:
    """Prepare a bus configuration, handling demo and 'other' interface modes.

    :param bus_config: Raw bus configuration from the buses array
    :param demo_dbc_path: Path to demo DBC file
    :return: Prepared configuration dict
    """
    config = bus_config.copy()
    bus_name = config.get("name", "bus")

    # Handle demo interface selection
    if config.get("interface") == "demo":
        logger.info(f"[{bus_name}] Demo mode: using built-in EV simulator")
        config["demo_mode"] = True
        config["interface"] = "virtual"
        config["channel"] = "vcan0"
        config["database_file"] = str(demo_dbc_path)
        config["receive_own_messages"] = True
        config["log_raw_frames"] = True

    # Handle "other" interface - merge config_json into main config
    if config.get("interface") == "other":
        logger.info(f"[{bus_name}] Using custom interface from config_json")

        if "config_json" not in config or not config["config_json"]:
            logger.error(
                f"[{bus_name}] 'other' interface requires config_json with interface and channel"
            )
            sys.exit(1)
        try:
            custom_config = json.loads(config["config_json"])
            if "interface" not in custom_config:
                logger.error(f"[{bus_name}] config_json must include 'interface' key")
                sys.exit(1)
            if "channel" not in custom_config:
                logger.error(f"[{bus_name}] config_json must include 'channel' key")
                sys.exit(1)
            # Merge custom config into main config
            config["interface"] = custom_config.pop("interface")
            config["channel"] = custom_config.pop("channel")
            # Update config_json with remaining custom parameters
            config["config_json"] = json.dumps(custom_config) if custom_config else ""
            logger.info(
                f"[{bus_name}] Custom interface: {config['interface']}, "
                f"channel: {config['channel']}"
            )
        except json.JSONDecodeError as e:
            logger.error(f"[{bus_name}] Invalid JSON in config_json: {e}")
            sys.exit(1)

    return config


def _create_codecs(config: dict, demo_dbc_path: Path) -> list[tuple[CanCodec, str]]:
    """Create CanCodec instances for all buses in the configuration.

    :param config: Full configuration dict with 'buses' array
    :param demo_dbc_path: Path to demo DBC file
    :return: List of (codec, action_registry_name) tuples
    """
    codecs: list[tuple[CanCodec, str]] = []

    buses = config.get("buses", [])
    if not buses:
        logger.error("No buses configured. Add at least one bus to the 'buses' array.")
        sys.exit(1)

    is_multi_bus = len(buses) > 1
    seen_names: set[str] = set()

    for i, bus_config in enumerate(buses):
        # Prepare the bus config first (handles demo mode and 'other' interface)
        # This gives us the actual channel name for defaulting
        prepared_config = _prepare_bus_config(bus_config, demo_dbc_path)

        # Determine bus name:
        # - Explicit name from config takes priority
        # - Multi-bus without name: default to channel name
        # - Single bus without name: None (backward compatible "can_codec")
        explicit_name = bus_config.get("name", "").strip() or None
        if explicit_name:
            bus_name = explicit_name
        elif is_multi_bus:
            # Default to channel name for multi-bus setups
            bus_name = prepared_config.get("channel", f"bus{i}")
            logger.info(f"Bus {i + 1}: no name specified, defaulting to '{bus_name}'")
        else:
            # Single bus without name: backward compatible
            bus_name = None

        # Validate uniqueness for multi-bus setups
        if is_multi_bus:
            if bus_name in seen_names:
                logger.error(f"Duplicate bus name '{bus_name}'. Each bus must have a unique name.")
                sys.exit(1)
            seen_names.add(bus_name)

        # Create codec:
        # - Single bus without name: bus_name=None → trace source "can_codec" (backward compatible)
        # - Otherwise: bus_name=name → trace source "{name}_can"
        codec = CanCodec(prepared_config, bus_name=bus_name)

        # Action registry name: use exact bus_name, or "can_codec" for backward compat
        action_name = bus_name if bus_name else "can_codec"
        codecs.append((codec, action_name))

        display_name = bus_name or "(default)"
        logger.info(
            f"Created bus codec: {display_name} "
            f"({prepared_config['interface']}:{prepared_config.get('channel', 'N/A')})"
        )

    return codecs


async def _run_codecs_async(codecs: list[CanCodec]) -> None:
    """Run multiple codecs concurrently.

    :param codecs: List of CanCodec instances to run
    """
    # Start all buses
    for codec in codecs:
        codec.start()

    # Run all codecs concurrently using their async run method
    tasks = [asyncio.create_task(codec._run_async()) for codec in codecs]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Codec tasks cancelled")
    finally:
        # Ensure all buses are stopped
        for codec in codecs:
            codec.stop()


def run_app_mode(demo: bool, file: Path | None, demo_dbc_path: Path) -> None:
    """Run CAN extension in app-based configuration mode.

    :param demo: Enable demo mode (adds a demo bus if no buses configured)
    :param file: Optional output file for trace recording
    :param demo_dbc_path: Path to demo DBC file
    """
    # Load and validate configuration
    config = load_config()

    # Apply log level from config (global setting)
    log_level_str = config.get("log_level", "INFO")
    try:
        log_level = getattr(logging, log_level_str)
        logging.getLogger().setLevel(log_level)
        logger.info(f"Log level set to: {log_level_str}")
    except AttributeError:
        logger.warning(f"Invalid log level '{log_level_str}', using INFO")
        logging.getLogger().setLevel(logging.INFO)

    # If demo flag is set and no buses configured, add a demo bus
    if demo:
        logger.info("Demo mode enabled via --demo flag")
        if not config.get("buses"):
            config["buses"] = [{"name": "demo", "interface": "demo"}]
        else:
            # Add demo bus to existing buses
            config["buses"].append({"name": "demo", "interface": "demo"})

    # Determine output file if --file was specified
    output_file = None
    if file is not None:
        # If --file was given without a value, use UTC timestamp
        if str(file) == ".":
            utc_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            file = Path(f"{utc_timestamp}.trz")
        output_file = file
        logger.info(f"Recording trace to: {output_file}")

    # Create all CAN codecs from buses array
    codec_pairs = _create_codecs(config, demo_dbc_path)
    codecs = [codec for codec, _ in codec_pairs]

    # Register interactive actions for each codec
    for codec, action_name in codec_pairs:
        zelos_sdk.actions_registry.register(codec, action_name)

    # Initialize SDK
    zelos_sdk.init(name="can", log_level="info", actions=True)

    # Setup shutdown handler for all codecs
    for codec in codecs:
        setup_shutdown_handler(codec)

    # Log startup info
    bus_count = len(codecs)
    logger.info(f"Starting CAN extension with {bus_count} bus{'es' if bus_count > 1 else ''}")

    # Run with optional trace writer
    if output_file:
        with zelos_sdk.TraceWriter(str(output_file)):
            asyncio.run(_run_codecs_async(codecs))
    else:
        asyncio.run(_run_codecs_async(codecs))
