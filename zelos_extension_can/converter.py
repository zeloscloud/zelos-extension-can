"""CAN trace file converter.

Converts various CAN log formats (.asc, .blf, .trc, etc.) to Zelos trace format.
Keeps it simple - no complex state management, just pure conversion.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import can
import zelos_sdk

logger = logging.getLogger(__name__)

# Supported file extensions and their python-can reader classes
SUPPORTED_FORMATS = {
    ".asc": "ASCReader",
    ".blf": "BLFReader",
    ".trc": "TRCReader",
    ".log": "CanutilsLogReader",
    ".csv": "CSVReader",
    ".mf4": "MF4Reader",
}


class ConversionStats:
    """Statistics from conversion process."""

    def __init__(self):
        self.messages_converted = 0
        self.messages_skipped = 0
        self.decode_errors = 0
        self.start_timestamp = None
        self.end_timestamp = None

    def to_dict(self) -> dict[str, Any]:
        """Convert stats to dictionary."""
        duration = None
        if self.start_timestamp is not None and self.end_timestamp is not None:
            duration = self.end_timestamp - self.start_timestamp

        return {
            "messages_converted": self.messages_converted,
            "messages_skipped": self.messages_skipped,
            "decode_errors": self.decode_errors,
            "duration_seconds": round(duration, 3) if duration else None,
        }


def _get_reader_config(input_file: Path) -> tuple[type, dict[str, Any]]:
    """Get CAN reader class and configuration for input file.

    Args:
        input_file: Source CAN log file

    Returns:
        Tuple of (reader_class, reader_kwargs)

    Raises:
        ValueError: If file format is unsupported
        ImportError: If required python-can reader is not available
    """
    suffix = input_file.suffix.lower()
    if suffix not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported format '{suffix}'. "
            f"Supported formats: {', '.join(SUPPORTED_FORMATS.keys())}"
        )

    # Get appropriate reader class
    reader_name = SUPPORTED_FORMATS[suffix]
    reader_class = getattr(can, reader_name, None)
    if reader_class is None:
        raise ImportError(
            f"CAN reader '{reader_name}' not available. Install with: pip install python-can"
        )

    # Reader-specific options to preserve original timestamps
    reader_kwargs = {}
    if suffix == ".asc":
        # Don't drop time information (python-can's default is relative_timestamp=True)
        # Setting to False preserves absolute timestamps from the file
        reader_kwargs["relative_timestamp"] = False

    return reader_class, reader_kwargs


def _make_decoder(
    database_file: Path,
    namespace: Any,
    emit_schemas_on_init: bool = True,
) -> Any:
    """Build the Rust-backed decoder a conversion runs on.

    Decoding happens in Rust with the GIL released, which is the whole point of
    routing conversions through `zelos_can` rather than the Python `CanCodec`.

    Two things are deliberate and load-bearing:

    - **Source name.** `can_codec` is what `CanCodec` produced for a conversion
      (no `bus_name` is passed on this path), and the source name *is* the
      leading segment of every signal path in the resulting trace. Changing it
      would silently re-path every future conversion and break saved layouts
      against previously converted traces.
    - **`timestamp_mode="absolute"`.** Preserves each frame's own timestamp
      verbatim. The Rust side treats `absolute` as an alias for `hardware`;
      anything it does not recognise falls back to `auto`, which would re-stamp
      a recording against wall-clock.
    """
    from zelos_can import CanDecoder

    return CanDecoder(
        database_file=str(database_file),
        timestamp_mode="absolute",
        emit_schemas_on_init=emit_schemas_on_init,
        source=zelos_sdk.TraceSource("can_codec", namespace=namespace),
    )


def _process_messages(
    reader: Any,
    decoder: Any,
    stats: ConversionStats,
    progress_callback: Callable[[int], None] | None = None,
) -> None:
    """Feed every frame through the decoder and track stats.

    Args:
        reader: CAN message reader iterator
        decoder: zelos_can.CanDecoder for decoding
        stats: ConversionStats to update
        progress_callback: Optional callback(message_count) for progress updates
    """
    # `metrics()` crosses into Rust and rebuilds a snapshot, so it is read on
    # the logging/callback cadence rather than once per frame. The old code
    # paid three of those per message.
    # Timestamps are passed explicitly rather than via `decode_message`, which
    # treats 0.0 as "no hardware timestamp" and substitutes wall-clock now. That
    # is right for a live bus, where python-can reports 0.0 when the driver gives
    # nothing, and wrong for file replay, where 0.0 is the first frame of any
    # relative-timestamped capture. Left alone, converting a candump whose first
    # line reads `(0.000000)` produces a trace spanning from that frame's
    # wall-clock stamp to the rest of the file's epoch times.
    decode = decoder.decode_frame
    last_log_count = 0
    for seen, can_msg in enumerate(reader, start=1):
        ts = can_msg.timestamp
        decode(
            arbitration_id=can_msg.arbitration_id,
            data=bytes(can_msg.data),
            timestamp_ns=None if ts is None else int(ts * 1e9),
            is_extended=can_msg.is_extended_id,
            is_fd=can_msg.is_fd,
            is_remote_frame=can_msg.is_remote_frame,
        )

        # Track timing. `is not None` rather than truthiness: a 0.0 stamp is a
        # real first frame, not a missing one.
        if ts is not None:
            if stats.start_timestamp is None:
                stats.start_timestamp = ts
            stats.end_timestamp = ts

        if seen % 1000 == 0:
            metrics = decoder.metrics()
            stats.messages_converted = metrics.messages_decoded
            stats.messages_skipped = metrics.unknown_messages
            stats.decode_errors = metrics.decode_errors

            if progress_callback:
                progress_callback(stats.messages_converted)

            if metrics.messages_received - last_log_count >= 100000:
                logger.info(
                    f"Progress: {metrics.messages_received:,} received, "
                    f"{stats.messages_converted:,} decoded, "
                    f"{stats.messages_skipped:,} skipped, {stats.decode_errors:,} errors"
                )
                last_log_count = metrics.messages_received

    # Final read so a run shorter than the sampling interval still reports.
    metrics = decoder.metrics()
    stats.messages_converted = metrics.messages_decoded
    stats.messages_skipped = metrics.unknown_messages
    stats.decode_errors = metrics.decode_errors


def convert_can_trace(
    input_file: Path,
    database_file: Path,
    output_file: Path,
    progress_callback: Callable[[int], None] | None = None,
    emit_schemas_on_init: bool = True,
) -> ConversionStats:
    """Convert CAN trace file to Zelos trace format.

    Timestamps are preserved exactly as they appear in the source file,
    with no adjustments or offsets applied. This ensures the output trace
    maintains the same timing characteristics as the original recording.

    Uses a local CanCodec instance to handle all decoding complexities including
    named signal values, multiplexed messages, and error handling.

    Args:
        input_file: Source CAN log file (.asc, .blf, .trc, etc.)
        database_file: CAN database file for decoding (.dbc, .arxml, .kcd, .sym)
        output_file: Destination .trz file
        progress_callback: Optional callback(message_count) for progress updates
        emit_schemas_on_init: Pre-generate all schemas before processing

    Returns:
        ConversionStats object with conversion statistics

    Raises:
        FileNotFoundError: If input or database file doesn't exist
        ValueError: If file format is unsupported
        ImportError: If required python-can reader is not available
    """
    # Validate inputs
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")
    if not database_file.exists():
        raise FileNotFoundError(f"CAN database file not found: {database_file}")

    # Get reader configuration
    reader_class, reader_kwargs = _get_reader_config(input_file)

    stats = ConversionStats()

    logger.info(f"Converting {input_file} -> {output_file}")

    # Create isolated namespace to avoid mixing converted data with live data
    # Each conversion gets its own namespace with isolated router
    converter_namespace = zelos_sdk.TraceNamespace("converter")

    # Create local, isolated trace writer and source in the namespace
    with zelos_sdk.TraceWriter(str(output_file), namespace=converter_namespace):
        decoder = _make_decoder(database_file, converter_namespace, emit_schemas_on_init)

        # Create reader and process messages
        reader = reader_class(str(input_file), **reader_kwargs)
        _process_messages(reader, decoder, stats, progress_callback)

        # Push buffered events through to the writer before it closes. This is
        # the real flush the old `time.sleep(2.0)` was approximating: a sleep
        # both wasted two seconds on every conversion and never actually
        # guaranteed the tail had landed.
        decoder.flush()

    logger.info(f"Conversion complete: {stats.to_dict()}")
    return stats


def main():
    """CLI entry point for CAN trace conversion."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Convert CAN trace files to Zelos format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic conversion (output defaults to input.trz)
  python -m zelos_extension_can.converter capture.asc decoder.dbc

  # Specify output file
  python -m zelos_extension_can.converter capture.blf decoder.dbc -o output.trz

  # Overwrite existing file
  python -m zelos_extension_can.converter capture.trc decoder.dbc -f

Supported formats: .asc, .blf, .trc, .log, .csv, .mf4
        """,
    )

    parser.add_argument("input_file", type=Path, help="CAN log file to convert")
    parser.add_argument(
        "database_file", type=Path, help="CAN database file (.dbc, .arxml, .kcd, .sym)"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output .trz file (default: input_file.trz)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite output file if it exists",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )

    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
    )

    # Validate input file
    if not args.input_file.exists():
        logger.error(f"Input file not found: {args.input_file}")
        sys.exit(1)

    # Validate database file
    if not args.database_file.exists():
        logger.error(f"CAN database file not found: {args.database_file}")
        sys.exit(1)

    # Determine output file
    output_file = args.output if args.output else args.input_file.with_suffix(".trz")

    # Check if output exists
    if output_file.exists():
        if args.force:
            logger.info(f"Removing existing file: {output_file}")
            output_file.unlink()
        else:
            logger.error(f"Output file exists: {output_file} (use -f to overwrite)")
            sys.exit(1)

    # Validate format
    if args.input_file.suffix.lower() not in SUPPORTED_FORMATS:
        logger.error(
            f"Unsupported format: {args.input_file.suffix}\n"
            f"Supported formats: {', '.join(SUPPORTED_FORMATS.keys())}"
        )
        sys.exit(1)

    try:
        # Perform conversion with tqdm progress bar if available
        _convert_with_progress(args.input_file, args.database_file, output_file, args.verbose)

    except Exception as e:
        logger.error(f"Conversion failed: {e}")
        if args.verbose:
            raise
        sys.exit(1)


def _convert_with_progress(
    input_file: Path, database_file: Path, output_file: Path, verbose: bool
) -> None:
    """Convert CAN trace with optional progress bar (CLI wrapper).

    Args:
        input_file: Source CAN log file
        database_file: CAN database file for decoding
        output_file: Destination .trz file
        verbose: Enable verbose output
    """
    # Try to import tqdm for progress bar
    try:
        from tqdm import tqdm
        from tqdm.contrib.logging import logging_redirect_tqdm

        has_tqdm = True
    except ImportError:
        has_tqdm = False
        if verbose:
            logger.info("Install tqdm for progress bar: pip install zelos-extension-can[cli]")

    # Get reader configuration
    reader_class, reader_kwargs = _get_reader_config(input_file)

    # Count lines in file for progress bar total (approximate)
    file_lines = None
    if has_tqdm:
        logger.info("Counting lines in file...")
        with input_file.open("r", encoding="utf-8", errors="ignore") as f:
            file_lines = sum(1 for _ in f)

    logger.info(f"Converting {input_file} -> {output_file}")
    logger.info(f"Using database: {database_file}")

    # Create isolated namespace for conversion
    converter_namespace = zelos_sdk.TraceNamespace("converter")
    stats = ConversionStats()

    # Create local, isolated trace writer and codec in the namespace
    with zelos_sdk.TraceWriter(str(output_file), namespace=converter_namespace):
        decoder = _make_decoder(database_file, converter_namespace)
        reader = reader_class(str(input_file), **reader_kwargs)

        # Wrap reader with tqdm if available
        if has_tqdm:
            with logging_redirect_tqdm():
                reader_iter = tqdm(reader, total=file_lines, unit="msg")
                _process_messages(reader_iter, decoder, stats)
        else:
            _process_messages(reader, decoder, stats)

        decoder.flush()

    # Print results to console
    print("\n✓ Conversion complete!")
    print(f"  Input:     {input_file}")
    print(f"  Output:    {output_file}")
    print(f"  Database:  {database_file}")
    print(f"\n  Messages:  {stats.messages_converted:,} converted")
    print(f"             {stats.messages_skipped:,} skipped")
    print(f"             {stats.decode_errors:,} errors")


if __name__ == "__main__":
    main()
