"""Trace file conversion and export actions."""

import logging
from pathlib import Path
from typing import Any

from zelos_sdk.actions import action

from .registry import all_buses, get_codec

logger = logging.getLogger(__name__)


@action("Convert Trace File", "Convert CAN log to Zelos trace format")
@action.select("bus", choices=all_buses, title="Bus (for default DBC)")
@action.text(
    "input_path",
    title="Input File Path",
    description="Path to CAN log file (.asc, .blf, .trc, etc.)",
    widget="file-picker",
)
@action.text(
    "output_path",
    required=False,
    default="",
    title="Output File Path",
    description="Output .trz file path (optional, defaults to input name with .trz)",
    placeholder="e.g., /path/to/output.trz",
)
@action.text(
    "database_path",
    required=False,
    default="",
    title="CAN Database File (.dbc)",
    description="Override database file (optional, defaults to bus's configured file)",
    placeholder="Leave empty to use bus's database",
    widget="file-picker",
)
@action.boolean(
    "overwrite", required=False, default=False, title="Overwrite if exists", widget="toggle"
)
@action.boolean(
    "emit_all_schemas",
    required=False,
    default=True,
    title="Emit all schemas",
    description="Emit all schemas before processing. "
    "Disable for faster startup with large databases.",
    widget="toggle",
)
def convert_trace_file(
    bus: str,
    input_path: str,
    output_path: str = "",
    database_path: str = "",
    overwrite: bool = False,
    emit_all_schemas: bool = True,
) -> dict[str, Any]:
    """Convert CAN trace file to Zelos format using CAN database file."""
    from zelos_extension_can.converter import convert_can_trace

    codec = get_codec(bus)
    if not codec:
        return {"status": "error", "message": f"Bus '{bus}' not found"}

    try:
        input_file = Path(input_path).expanduser().resolve()
        if not input_file.exists():
            return {"status": "error", "message": f"Input file not found: {input_file}"}

        # Resolve database file
        if database_path:
            database_file = Path(database_path).expanduser().resolve()
            if not database_file.exists():
                return {
                    "status": "error",
                    "message": f"CAN database file not found: {database_file}",
                }
            logger.info("Using user-specified database: %s", database_file)
        else:
            if not codec.database_file_path:
                return {
                    "status": "error",
                    "message": "No database file specified and bus has none configured",
                }
            database_file = Path(codec.database_file_path)
            logger.info("Using bus's configured database: %s", database_file)

        # Resolve output path
        if not output_path:
            output_path = str(input_file.with_suffix(".trz"))
        output_file = Path(output_path).expanduser().resolve()
        if output_file.suffix.lower() != ".trz":
            output_file = output_file.with_suffix(".trz")

        if output_file == input_file:
            return {
                "status": "error",
                "message": f"Output cannot be the same as input: {input_file}",
            }

        if output_file.exists():
            if overwrite:
                output_file.unlink()
            else:
                return {
                    "status": "error",
                    "message": f"Output file '{output_file}' already exists. "
                    "Enable 'Overwrite if exists' to replace it.",
                }

        logger.info(
            "Converting %s -> %s using database: %s", input_file, output_file, database_file
        )
        stats = convert_can_trace(
            input_file, database_file, output_file, emit_schemas_on_init=emit_all_schemas
        )

        return {
            "status": "success",
            "input_file": str(input_file),
            "database_file": str(database_file),
            "output_file": str(output_file),
            **stats.to_dict(),
        }

    except FileNotFoundError as e:
        return {"status": "error", "message": f"File not found: {e}"}
    except ValueError as e:
        return {"status": "error", "message": f"Invalid input: {e}"}
    except ImportError as e:
        return {"status": "error", "message": f"Missing dependency: {e}"}
    except Exception as e:
        logger.exception("Conversion failed")
        return {"status": "error", "message": f"Conversion failed: {e}"}


@action("Export Trace to Log", "Export raw CAN frames from TRZ to candump log format")
@action.text(
    "input_path",
    title="Input TRZ File",
    description="Path to Zelos trace file (.trz) with raw CAN frames",
    widget="file-picker",
)
@action.text(
    "output_path",
    required=False,
    default="",
    title="Output Log File",
    description="Output .log file path (optional, defaults to input name with .log)",
    placeholder="e.g., /path/to/output.log",
)
@action.boolean(
    "overwrite", required=False, default=False, title="Overwrite if exists", widget="toggle"
)
def export_trace_to_log(
    input_path: str,
    output_path: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Export raw CAN frames from TRZ trace to candump log format.

    This action does not require a bus — it operates on trace files directly.
    """
    from zelos_extension_can.cli.export import export_to_candump

    try:
        input_file = Path(input_path).expanduser().resolve()
        if not input_file.exists():
            return {"status": "error", "message": f"Input file not found: {input_file}"}
        if input_file.suffix.lower() != ".trz":
            return {"status": "error", "message": f"Input file must be a .trz file: {input_file}"}

        if not output_path:
            output_path = str(input_file.with_suffix(".log"))
        output_file = Path(output_path).expanduser().resolve()
        if output_file.suffix.lower() != ".log":
            output_file = output_file.with_suffix(".log")

        if output_file == input_file:
            return {
                "status": "error",
                "message": f"Output cannot be the same as input: {input_file}",
            }

        if output_file.exists():
            if overwrite:
                output_file.unlink()
            else:
                return {
                    "status": "error",
                    "message": f"Output file '{output_file}' already exists. "
                    "Enable 'Overwrite if exists' to replace it.",
                }

        logger.info("Exporting %s -> %s", input_file, output_file)
        stats = export_to_candump(input_file, output_file)

        if stats["frame_count"] == 0:
            return {
                "status": "warning",
                "message": "No raw CAN frames found in trace. "
                "Ensure 'Log Raw CAN Frames' was enabled when recording.",
                "input_file": str(input_file),
                "sources_found": stats["sources_found"],
            }

        return {
            "status": "success",
            "input_file": str(input_file),
            "output_file": str(output_file),
            "frame_count": stats["frame_count"],
            "sources_exported": stats["sources_exported"],
        }

    except FileNotFoundError as e:
        return {"status": "error", "message": f"File not found: {e}"}
    except Exception as e:
        logger.exception("Export failed")
        return {"status": "error", "message": f"Export failed: {e}"}
