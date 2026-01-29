"""Export command for extracting raw CAN frames from TRZ trace files."""

import logging
import sys
from pathlib import Path

import pyarrow as pa
import rich_click as click
import zelos_sdk

logger = logging.getLogger(__name__)


def _find_raw_sources(reader: zelos_sdk.TraceReader) -> list[tuple[str, str]]:
    """Find all can_raw sources in the trace file.

    :param reader: Open TraceReader instance
    :return: List of (segment_id, source_name) tuples for raw CAN sources
    """
    raw_sources = []

    for segment in reader.list_data_segments():
        segment_id = segment.id
        sources = reader.list_fields(segment_id)

        # Look for sources ending in _raw or exactly can_raw
        for source in sources:
            source_name = source.name
            if source_name == "can_raw" or source_name.endswith("_raw"):
                # Verify it has the expected messages event with required fields
                event_names = [e.name for e in source.events]
                if "messages" in event_names:
                    msg_event = next(e for e in source.events if e.name == "messages")
                    field_names = [f.name for f in msg_event.fields]
                    if all(f in field_names for f in ["arbitration_id", "dlc", "data"]):
                        raw_sources.append((segment_id, source_name))

    return raw_sources


def _format_candump_line(timestamp_ns: int, channel: str, arb_id: int, data: bytes) -> str:
    """Format a single CAN frame in candump log format.

    Format: (timestamp) channel arbid#data
    Example: (1234567890.123456) vcan0 1F5#01020304050607FF

    :param timestamp_ns: Timestamp in nanoseconds
    :param channel: CAN channel name (e.g., vcan0, can0)
    :param arb_id: CAN arbitration ID
    :param data: CAN frame data bytes
    :return: Formatted candump log line
    """
    timestamp_sec = timestamp_ns / 1_000_000_000
    data_hex = data.hex().upper()
    return f"({timestamp_sec:.6f}) {channel} {arb_id:03X}#{data_hex}"


def export_to_candump(input_file: Path, output_file: Path) -> dict:
    """Export raw CAN frames from TRZ file to candump log format.

    :param input_file: Source TRZ trace file
    :param output_file: Destination .log file
    :return: Statistics dict with frame_count, sources_found, etc.
    """
    stats = {
        "frame_count": 0,
        "sources_found": [],
        "sources_exported": [],
    }

    with zelos_sdk.TraceReader(str(input_file)) as reader:
        # Find all raw CAN sources
        raw_sources = _find_raw_sources(reader)
        stats["sources_found"] = [s[1] for s in raw_sources]

        if not raw_sources:
            logger.error("No raw CAN sources found in trace file")
            logger.error("Ensure 'Log Raw CAN Frames' was enabled when recording the trace")
            return stats

        # Get time range for queries
        time_range = reader.time_range()

        # Collect all frames with timestamps for sorting
        all_frames = []

        for segment_id, source_name in raw_sources:
            logger.info(f"Reading from source: {source_name}")
            stats["sources_exported"].append(source_name)

            # Derive channel name from source: can_raw -> can0, vcan0_raw -> vcan0
            chan = "can0" if source_name == "can_raw" else source_name.rsplit("_raw", 1)[0]

            # Query for raw frame data
            fields = [
                f"*/{source_name}/messages.arbitration_id",
                f"*/{source_name}/messages.dlc",
                f"*/{source_name}/messages.data",
            ]

            result = reader.query(
                data_segment_ids=[segment_id],
                fields=fields,
                start=time_range.start,
                end=time_range.end,
            )

            # Convert to PyArrow table
            table = pa.ipc.open_stream(result.to_arrow()).read_all()

            if table.num_rows == 0:
                logger.info(f"  No frames in {source_name}")
                continue

            # Get column names (they include the full path with segment UUID)
            col_names = table.column_names
            arb_col = next((c for c in col_names if c.endswith(".arbitration_id")), None)
            data_col = next((c for c in col_names if c.endswith(".data")), None)

            # Time column is 'time_s' (seconds as double) from the trace
            time_col = "time_s" if "time_s" in col_names else None

            if not arb_col or not data_col:
                logger.warning(f"  Missing required columns in {source_name}")
                continue

            # Extract data
            arb_ids = table.column(arb_col).to_pylist()
            data_values = table.column(data_col).to_pylist()

            # Get timestamps from time_s column (seconds as float)
            if time_col:
                timestamps_sec = table.column(time_col).to_pylist()
            else:
                # No timestamp found, generate sequential timestamps
                logger.warning("  No timestamps found, using sequential values")
                timestamps_sec = [float(i) for i in range(len(arb_ids))]

            # Collect frames
            for ts_sec, arb_id, data in zip(timestamps_sec, arb_ids, data_values, strict=True):
                # Convert seconds to nanoseconds
                ts_ns = int(ts_sec * 1_000_000_000)

                # Handle data as bytes
                if isinstance(data, bytes):
                    data_bytes = data
                elif hasattr(data, "as_py"):
                    data_bytes = data.as_py()
                else:
                    data_bytes = bytes(data) if data else b""

                all_frames.append((ts_ns, chan, arb_id, data_bytes))

            logger.info(f"  Read {len(arb_ids)} frames from {source_name}")

        # Sort all frames by timestamp
        all_frames.sort(key=lambda x: x[0])

        # Write output file
        with output_file.open("w") as f:
            for ts_ns, chan, arb_id, data_bytes in all_frames:
                line = _format_candump_line(ts_ns, chan, arb_id, data_bytes)
                f.write(line + "\n")
                stats["frame_count"] += 1

    return stats


@click.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output .log file (default: input_file.log)",
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
def export(
    input_file: Path,
    output: Path | None,
    force: bool,
    verbose: bool,
) -> None:
    """Export raw CAN frames from TRZ trace to candump log format.

    This command extracts raw CAN frames from a Zelos trace file (.trz)
    and writes them in candump log format (.log), which can be replayed
    or re-converted with a different DBC file.

    **Requirements:** The source trace must have been recorded with
    'Log Raw CAN Frames' enabled.

    **Output format (candump):**

        (timestamp) channel arbid#data

    **Examples:**

      # Basic export (output defaults to input.log)

      zelos-extension-can export recording.trz

      # Specify output file

      zelos-extension-can export recording.trz -o frames.log

    **Workflow for re-decoding with a new DBC:**

      1. Export: zelos-extension-can export recording.trz -o frames.log

      2. Convert: zelos-extension-can convert frames.log new_decoder.dbc -o new.trz
    """
    # Setup logging
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
    )

    # Determine output file
    output_file = output if output else input_file.with_suffix(".log")

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

    # Validate input is a .trz file
    if input_file.suffix.lower() != ".trz":
        click.echo(
            f"Warning: Input file does not have .trz extension: {input_file}",
            err=True,
        )

    try:
        logger.info(f"Exporting {input_file} -> {output_file}")
        stats = export_to_candump(input_file, output_file)

        # Print results
        if stats["frame_count"] > 0:
            print("\n✓ Export complete!")
            print(f"  Input:   {input_file}")
            print(f"  Output:  {output_file}")
            print(f"  Sources: {', '.join(stats['sources_exported'])}")
            print(f"  Frames:  {stats['frame_count']:,}")
        else:
            click.echo("\n✗ No raw CAN frames found in trace", err=True)
            click.echo(
                "  Ensure 'Log Raw CAN Frames' was enabled when recording the trace",
                err=True,
            )
            sys.exit(1)

    except Exception as e:
        logger.error(f"Export failed: {e}")
        if verbose:
            raise
        sys.exit(1)
