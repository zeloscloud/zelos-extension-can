"""Pure CLI trace command."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import rich_click as click
import zelos_sdk

from ..codec import CanCodec
from .utils import setup_shutdown_handler

logger = logging.getLogger(__name__)


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

    setup_shutdown_handler(codec)

    # Run with optional trace writer
    logger.info("Starting CAN trace...")
    if output_file:
        with zelos_sdk.TraceWriter(str(output_file)):
            codec.start()
            codec.run()
    else:
        codec.start()
        codec.run()
