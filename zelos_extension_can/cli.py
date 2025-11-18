"""CLI commands for zelos-extension-can."""

import json
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

import rich_click as click
import zelos_sdk
from zelos_sdk.extensions import load_config

from .codec import CanCodec

logger = logging.getLogger(__name__)


def _setup_shutdown_handler(codec: CanCodec) -> None:
    """Setup signal handlers for graceful shutdown.

    :param codec: CAN codec instance to stop on shutdown
    """

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

    _setup_shutdown_handler(codec)

    # Run with optional trace writer
    logger.info("Starting CAN extension")
    if output_file:
        with zelos_sdk.TraceWriter(str(output_file)):
            codec.start()
            codec.run()
    else:
        codec.start()
        codec.run()


@click.command()
@click.argument("interface", type=str)
@click.argument("channel", type=str)
@click.argument("database_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--bitrate",
    type=int,
    default=500000,
    help="CAN bus bitrate (default: 500000)",
)
@click.option(
    "--file",
    type=click.Path(path_type=Path),
    default=None,
    is_flag=False,
    flag_value=".",
    help="Record trace to .trz file (defaults to UTC.trz if no filename specified)",
)
@click.option(
    "--fd",
    is_flag=True,
    help="Enable CAN-FD mode",
)
@click.option(
    "--data-bitrate",
    type=int,
    help="CAN-FD data phase bitrate",
)
def trace(
    interface: str,
    channel: str,
    database_file: Path,
    bitrate: int,
    file: Path | None,
    fd: bool,
    data_bitrate: int | None,
) -> None:
    """Trace CAN bus without app configuration.

    Pure CLI mode for tracing a CAN bus. Specify all parameters directly.

    Examples:

      # Trace SocketCAN interface

      zelos-extension-can trace socketcan can0 vehicle.dbc

      # Trace with custom bitrate

      zelos-extension-can trace pcan PCAN_USBBUS1 vehicle.dbc --bitrate 250000

      # Trace and record to file

      zelos-extension-can trace socketcan can0 vehicle.dbc --file my_trace.trz

      # Trace CAN-FD

      zelos-extension-can trace socketcan can0 vehicle.dbc --fd --data-bitrate 2000000
    """
    # Build config from CLI arguments
    config = {
        "interface": interface,
        "channel": channel,
        "database_file": str(database_file),
        "bitrate": bitrate,
        "fd": fd,
        "log_raw_frames": True,  # Enable raw logging in CLI mode
        "emit_schemas_on_init": True,  # Emit all schemas upfront in CLI mode
    }

    if data_bitrate:
        config["data_bitrate"] = data_bitrate

    logger.info(f"Tracing {interface} interface on {channel}")
    logger.info(f"Database: {database_file}")
    logger.info(f"Bitrate: {bitrate}")
    if fd:
        logger.info(f"CAN-FD enabled, data bitrate: {data_bitrate}")

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

    _setup_shutdown_handler(codec)

    # Run with optional trace writer
    logger.info("Starting CAN trace...")
    if output_file:
        with zelos_sdk.TraceWriter(str(output_file)):
            codec.start()
            codec.run()
    else:
        codec.start()
        codec.run()


@click.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.argument("database_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output .trz file (default: input_file.trz)",
)
@click.option(
    "-f",
    "--force",
    is_flag=True,
    help="Overwrite output file if it exists",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Verbose output with debug logging",
)
def convert(
    input_file: Path,
    database_file: Path,
    output: Path | None,
    force: bool,
    verbose: bool,
) -> None:
    """Convert CAN log files to Zelos trace format.

    Supported formats: .asc, .blf, .trc, .log, .csv, .mf4

    Examples:

      # Basic conversion (output defaults to input.trz)

      zelos-extension-can convert capture.asc decoder.dbc

      # Specify output file

      zelos-extension-can convert capture.blf decoder.dbc -o output.trz

      # Overwrite existing file

      zelos-extension-can convert capture.trc decoder.dbc -f
    """
    from .converter import SUPPORTED_FORMATS, _convert_with_progress

    # Setup logging
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
    )

    # Determine output file
    output_file = output if output else input_file.with_suffix(".trz")

    # Check if output exists
    if output_file.exists():
        if force:
            logger.info(f"Removing existing file: {output_file}")
            output_file.unlink()
        else:
            click.echo(
                f"Error: Output file exists: {output_file} (use -f/--force to overwrite)",
                err=True,
            )
            sys.exit(1)

    # Validate format
    if input_file.suffix.lower() not in SUPPORTED_FORMATS:
        click.echo(
            f"Error: Unsupported format: {input_file.suffix}\n"
            f"Supported formats: {', '.join(SUPPORTED_FORMATS.keys())}",
            err=True,
        )
        sys.exit(1)

    try:
        # Perform conversion with progress bar
        _convert_with_progress(input_file, database_file, output_file, verbose)
    except Exception as e:
        logger.error(f"Conversion failed: {e}")
        if verbose:
            raise
        sys.exit(1)
