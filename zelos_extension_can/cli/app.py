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


def _prepare_bus_config(
    config: dict, demo_dbc_path: Path, is_primary: bool = True
) -> dict:
    """Prepare a bus configuration, handling demo and 'other' interface modes.

    :param config: Raw bus configuration
    :param demo_dbc_path: Path to demo DBC file
    :param is_primary: Whether this is the primary bus (for demo mode handling)
    :return: Prepared configuration dict
    """
    # Handle demo interface selection (only for primary bus)
    if is_primary and config.get("interface") == "demo":
        logger.info("Demo mode: using built-in EV simulator")
        config["demo_mode"] = True
        config["interface"] = "virtual"
        config["channel"] = "vcan0"
        config["database_file"] = str(demo_dbc_path)
        config["receive_own_messages"] = True
        config["log_raw_frames"] = True

    # Handle "other" interface - merge config_json into main config
    if config.get("interface") == "other":
        bus_name = config.get("name", "bus")
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


def _create_codecs(
    config: dict, demo_dbc_path: Path
) -> list[tuple[CanCodec, str]]:
    """Create CanCodec instances for primary and additional buses.

    :param config: Full configuration dict (may include additional_buses)
    :param demo_dbc_path: Path to demo DBC file
    :return: List of (codec, action_registry_name) tuples
    """
    codecs: list[tuple[CanCodec, str]] = []

    # Prepare primary bus config
    primary_config = _prepare_bus_config(config.copy(), demo_dbc_path, is_primary=True)

    # Determine bus name for primary (use 'name' if provided, else None for default behavior)
    primary_name = config.get("name") or None

    # Create primary codec
    primary_codec = CanCodec(primary_config, bus_name=primary_name)
    action_name = f"{primary_name}_can" if primary_name else "can_codec"
    codecs.append((primary_codec, action_name))
    logger.info(
        f"Created primary bus codec: {primary_name or 'default'} "
        f"({primary_config['interface']}:{primary_config['channel']})"
    )

    # Create codecs for additional buses
    additional_buses = config.get("additional_buses", [])
    for i, bus_config in enumerate(additional_buses):
        # Each additional bus must have a name
        bus_name = bus_config.get("name")
        if not bus_name:
            logger.error(f"Additional bus {i + 1} missing required 'name' field")
            sys.exit(1)

        # Prepare the bus config
        prepared_config = _prepare_bus_config(bus_config.copy(), demo_dbc_path, is_primary=False)

        # Create codec with bus_name for trace source prefixing
        codec = CanCodec(prepared_config, bus_name=bus_name)
        action_name = f"{bus_name}_can"
        codecs.append((codec, action_name))
        logger.info(
            f"Created additional bus codec: {bus_name} "
            f"({prepared_config['interface']}:{prepared_config['channel']})"
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

    # Determine output file if --file was specified
    output_file = None
    if file is not None:
        # If --file was given without a value, use UTC timestamp
        if str(file) == ".":
            utc_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            file = Path(f"{utc_timestamp}.trz")
        output_file = file
        logger.info(f"Recording trace to: {output_file}")

    # Create all CAN codecs (primary + additional buses)
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
