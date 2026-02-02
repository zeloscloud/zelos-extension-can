"""Export command for extracting raw CAN frames from TRZ trace files."""

import logging
import sys
from pathlib import Path

import pyarrow as pa
import rich_click as click
import zelos_sdk

logger = logging.getLogger(__name__)


def _find_raw_sources(reader: zelos_sdk.TraceReader) -> list[tuple[str, str, str]]:
    """Find all sources containing raw CAN frame data.

    Searches for any event with the required CAN frame signals (arbitration_id, dlc, data),
    regardless of source or event naming convention. This supports:
    - New format: can_raw/messages, {bus}_raw/messages
    - Old format: can#-link/rx, etc.

    :param reader: Open TraceReader instance
    :return: List of (segment_id, source_name, event_name) tuples for raw CAN sources
    """
    raw_sources = []
    required_fields = {"arbitration_id", "dlc", "data"}

    for segment in reader.list_data_segments():
        segment_id = segment.id
        sources = reader.list_fields(segment_id)

        for source in sources:
            source_name = source.name
            for event in source.events:
                field_names = {f.name for f in event.fields}
                # Check if this event has all required CAN frame fields
                if required_fields.issubset(field_names):
                    raw_sources.append((segment_id, source_name, event.name))
                    logger.debug(
                        f"Found CAN source: {source_name}/{event.name} "
                        f"(fields: {', '.join(sorted(field_names))})"
                    )

    return raw_sources


def _derive_channel_name(source_name: str, _event_name: str = "") -> str:
    """Derive CAN channel name from source/event naming.

    Handles various naming conventions:
    - can_raw -> can0
    - vcan0_raw -> vcan0
    - can0-link -> can0
    - vehicle_raw -> vehicle

    :param source_name: Trace source name
    :param _event_name: Event name (reserved for future use)
    :return: Channel name to use in candump log
    """
    # Try common suffixes
    for suffix in ("_raw", "-link", "-raw"):
        if source_name.endswith(suffix):
            base = source_name[: -len(suffix)]
            # If base is just "can", default to "can0"
            return "can0" if base == "can" else base

    # If source name looks like a channel already (can0, vcan0, etc.), use it
    if source_name.startswith(("can", "vcan", "pcan", "slcan")):
        return source_name

    # Default: use source name as-is
    return source_name


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

    Supports both old and new trace formats by searching for any event
    containing the required CAN frame fields (arbitration_id, dlc, data).

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
        # Find all sources with CAN frame data
        raw_sources = _find_raw_sources(reader)
        stats["sources_found"] = list({s[1] for s in raw_sources})  # Unique source names

        if not raw_sources:
            logger.error("No CAN frame sources found in trace file")
            logger.error(
                "Looking for events with signals: arbitration_id, dlc, data\n"
                "  - New traces: enable 'Log Raw CAN Frames' when recording\n"
                "  - Old traces: should have can#-link/rx or similar"
            )
            return stats

        # Get time range for queries
        time_range = reader.time_range()

        # Collect all frames with timestamps for sorting
        all_frames = []

        for segment_id, source_name, event_name in raw_sources:
            source_path = f"{source_name}/{event_name}"
            logger.info(f"Reading from: {source_path}")
            stats["sources_exported"].append(source_path)

            # Derive channel name from source naming convention
            chan = _derive_channel_name(source_name, event_name)

            # Query for raw frame data
            fields = [
                f"*/{source_name}/{event_name}.arbitration_id",
                f"*/{source_name}/{event_name}.dlc",
                f"*/{source_name}/{event_name}.data",
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
                logger.info(f"  No frames in {source_path}")
                continue

            # Get column names (they include the full path with segment UUID)
            col_names = table.column_names
            arb_col = next((c for c in col_names if c.endswith(".arbitration_id")), None)
            data_col = next((c for c in col_names if c.endswith(".data")), None)

            # Time column is 'time_s' (seconds as double) from the trace
            time_col = "time_s" if "time_s" in col_names else None

            if not arb_col or not data_col:
                logger.warning(f"  Missing required columns in {source_path}")
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

            logger.info(f"  Read {len(arb_ids)} frames from {source_path}")

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

    **Supported trace formats:**

    - New traces: Enable 'Log Raw CAN Frames' when recording

    - Old traces: Automatically detects can#-link/rx or similar events

    The export searches for any event containing (arbitration_id, dlc, data)
    signals, regardless of source naming convention.

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
