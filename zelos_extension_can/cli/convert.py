"""Convert command for CAN log files."""

import logging
import sys
from pathlib import Path

import rich_click as click

logger = logging.getLogger(__name__)


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
    from ..converter import SUPPORTED_FORMATS, _convert_with_progress

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
