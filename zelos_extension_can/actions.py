"""Free-floating CAN action functions registered under `can/<name>`.

This is the standard pattern for multi-bus extensions: a single global namespace
keyed by a `codec` parameter, not per-bus action paths (`can/<bus>/<action>`).

- CLI/SDK consumers get one stable surface — `can.send_message` always exists,
  with the same shape, regardless of how many buses are configured.
- Bus discovery is explicit via `list_codecs`, not implicit via action-path
  scanning on the consumer side.
- Dynamic `choices=` reflects the currently-registered codecs at form-render time.

Functions in this module read from the shared `CAN_CODECS` registry, which
`cli/app.py` populates at startup as it brings up each `CanCodec` instance.

Free functions (not class methods) are used deliberately so that
`@action.select("codec", choices=_available_codecs)` can reference a module-level
callable — class-method `choices=self.codecs` doesn't work because `self` is not
bound at decoration time.
"""

from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zelos_sdk.actions import ActionsRegistry, action

if TYPE_CHECKING:
    from .codec import CanCodec

logger = logging.getLogger(__name__)

# Shared codec registry — populated by `cli/app.py` (and any other entrypoint
# that brings up a CanCodec instance and wants its actions exposed).
CAN_CODECS: dict[str, CanCodec] = {}


def _available_codecs(*_args: Any) -> list[str]:
    """`choices=` provider for the `codec` select field. Called at form-render
    time, so it reflects the live set of registered codecs."""
    return sorted(CAN_CODECS.keys())


def _get_codec(name: str) -> CanCodec:
    codec = CAN_CODECS.get(name)
    if codec is None:
        raise ValueError(f"Unknown CAN codec '{name}'. Available: {sorted(CAN_CODECS.keys())}")
    return codec


# ─── Discovery ──────────────────────────────────────────────────────────────


@action(
    "List Codecs",
    "Names of all CAN codecs (buses) currently registered on this extension. "
    "Consumers use this to discover what `codec` values the other actions accept.",
)
def list_codecs() -> dict[str, Any]:
    return {"codecs": _available_codecs()}


# ─── Per-bus state + DBC ────────────────────────────────────────────────────


@action(
    "Get TX State",
    "Stateless snapshot of one bus: its periodics and bus-health metrics.",
)
@action.select("codec", title="CAN bus", choices=_available_codecs)
def get_tx_state(codec: str) -> dict[str, Any]:
    return _get_codec(codec).get_tx_state()


@action(
    "List Messages",
    "DBC message summary list for a bus — names + identifiers only, no "
    "per-signal metadata. Use describe_message to fetch a specific message's "
    "full signal detail on demand.",
)
@action.select("codec", title="CAN bus", choices=_available_codecs)
def list_messages(codec: str) -> dict[str, Any]:
    return _get_codec(codec).list_messages()


@action(
    "Describe Message",
    "Full signal-level detail for a single DBC message (units, ranges, "
    "value tables, mux structure).",
)
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("message", title="DBC message name")
def describe_message(codec: str, message: str) -> dict[str, Any]:
    return _get_codec(codec).describe_message(message)


# ─── Send (raw) ─────────────────────────────────────────────────────────────


@action("Send Raw", "Send a one-shot raw CAN frame")
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("can_id", title="CAN ID (hex)", placeholder="0x100")
@action.text(
    "data", title="Data (hex bytes)", placeholder="01 02 03 04", required=False, default=""
)
@action.boolean(
    "is_extended", title="Extended ID (29-bit)", required=False, default=False, widget="toggle"
)
@action.boolean("is_fd", title="CAN FD", required=False, default=False, widget="toggle")
def send_raw(
    codec: str,
    can_id: str,
    data: str,
    is_extended: bool = False,
    is_fd: bool = False,
) -> dict[str, Any]:
    return _get_codec(codec).send_raw(can_id, data, is_extended, is_fd)


@action("Start Periodic Raw", "Start raw periodic transmission. Returns {task_id, replaced}.")
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("can_id", title="CAN ID (hex)", placeholder="0x100")
@action.text("data", title="Data (hex bytes)", placeholder="01 02 03 04")
@action.number(
    "period_ms", title="Period (ms)", minimum=1, maximum=60_000, required=False, default=100
)
@action.boolean("is_extended", title="Extended ID", required=False, default=False, widget="toggle")
@action.boolean("is_fd", title="CAN FD", required=False, default=False, widget="toggle")
def start_periodic_raw(
    codec: str,
    can_id: str,
    data: str,
    period_ms: int = 100,
    is_extended: bool = False,
    is_fd: bool = False,
) -> dict[str, Any]:
    return _get_codec(codec).start_periodic_raw(can_id, data, period_ms, is_extended, is_fd)


# ─── Send (DBC) ─────────────────────────────────────────────────────────────


@action("Send Message", "Send a one-shot DBC-encoded message")
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("message", title="DBC message name")
@action.text("signals_json", title="Signals (JSON object)", placeholder='{"Speed": 50}')
@action.text("mux", title="Multiplexer (optional)", required=False, default="")
def send_message(codec: str, message: str, signals_json: str, mux: str = "") -> dict[str, Any]:
    return _get_codec(codec).send_message(message, signals_json, mux)


@action(
    "Encode Preview",
    "Encode a DBC message without transmitting. Returns the bytes that send_message would emit.",
)
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("message", title="DBC message name")
@action.text("signals_json", title="Signals (JSON object)", placeholder='{"Speed": 50}')
@action.text("mux", title="Multiplexer (optional)", required=False, default="")
def encode_preview(codec: str, message: str, signals_json: str, mux: str = "") -> dict[str, Any]:
    return _get_codec(codec).encode_preview(message, signals_json, mux)


@action(
    "Start Periodic Message",
    "Start DBC-encoded periodic transmission. Returns {task_id, replaced}.",
)
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("message", title="DBC message name")
@action.text("signals_json", title="Signals (JSON object)", placeholder='{"Speed": 50}')
@action.number(
    "period_ms", title="Period (ms)", minimum=1, maximum=60_000, required=False, default=100
)
@action.text("mux", title="Multiplexer (optional)", required=False, default="")
def start_periodic_message(
    codec: str,
    message: str,
    signals_json: str,
    period_ms: int = 100,
    mux: str = "",
) -> dict[str, Any]:
    return _get_codec(codec).start_periodic_message(message, signals_json, period_ms, mux)


@action("Stop Periodic", "Stop a periodic task by its stable task_id (from start_periodic_*)")
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("task_id", title="Task ID")
def stop_periodic(codec: str, task_id: str) -> dict[str, Any]:
    return _get_codec(codec).stop_periodic(task_id)


# ─── Bus-agnostic file utilities ────────────────────────────────────────────
#
# These don't take a `codec` *because* they're file-in / file-out conversions.
# The DBC source is explicit via `database_path` for converter — if empty, the
# user must select a `codec` whose loaded DBC will be used as the conversion
# database. We don't silently default to "first registered codec" because that
# silently couples a file conversion to whichever bus happened to start first.


@action(
    "Convert Trace File",
    "Convert a CAN log (.asc / .blf / .trc / candump .log) to Zelos trace "
    "format (.trz). Provide either an explicit `database_path` OR a `codec` "
    "whose loaded DBC will be used.",
)
@action.text(
    "input_path",
    title="Input File Path",
    description="Path to CAN log file (.asc, .blf, .trc, etc.)",
    widget="file-picker",
)
@action.text(
    "database_path",
    required=False,
    default="",
    title="CAN Database File (.dbc)",
    description="Explicit database file. If empty, `codec` must be set.",
    placeholder="/path/to/file.dbc",
    widget="file-picker",
)
@action.select(
    "codec",
    required=False,
    default="",
    title="Codec (fallback DBC source)",
    description=(
        "Used only when `database_path` is empty — the named codec's DBC drives the conversion."
    ),
    choices=_available_codecs,
)
@action.text(
    "output_path",
    required=False,
    default="",
    title="Output File Path",
    description="Output .trz file path (optional, defaults to input name with .trz)",
    placeholder="e.g., /path/to/output.trz",
)
@action.boolean(
    "overwrite", required=False, default=False, title="Overwrite if exists", widget="toggle"
)
@action.boolean(
    "emit_all_schemas",
    required=False,
    default=True,
    title="Emit all schemas",
    description=(
        "Emit all schemas before processing. Disable for faster startup with large databases."
    ),
    widget="toggle",
)
def convert_trace_file(
    input_path: str,
    database_path: str = "",
    codec: str = "",
    output_path: str = "",
    overwrite: bool = False,
    emit_all_schemas: bool = True,
) -> dict[str, Any]:
    from .converter import convert_can_trace

    try:
        # Validate arguments before touching the filesystem so callers get a
        # clear "you need to pass X" error rather than a misleading
        # "input file not found" when the real problem is missing config.
        if database_path:
            database_file = Path(database_path).expanduser().resolve()
            if not database_file.exists():
                return {
                    "status": "error",
                    "message": f"CAN database file not found: {database_file}",
                }
            logger.info("Using user-specified database: %s", database_file)
        elif codec:
            # _get_codec raises ValueError on unknown codec — caught by the
            # outer ValueError handler below, which surfaces the message
            # with an "Invalid input:" prefix.
            database_file = Path(_get_codec(codec).database_file_path)
            logger.info("Using codec '%s' database: %s", codec, database_file)
        else:
            return {
                "status": "error",
                "message": "Provide either `database_path` or `codec`. Neither was given.",
            }

        input_file = Path(input_path).expanduser().resolve()
        if not input_file.exists():
            return {"status": "error", "message": f"Input file not found: {input_file}"}

        if not output_path:
            output_path = str(input_file.with_suffix(".trz"))
        output_file = Path(output_path).expanduser().resolve()
        if output_file.suffix.lower() != ".trz":
            output_file = output_file.with_suffix(".trz")

        if output_file == input_file:
            return {
                "status": "error",
                "message": f"Output file cannot be the same as input file: {input_file}",
            }

        if output_file.exists():
            if overwrite:
                logger.info("Removing existing file: %s", output_file)
                output_file.unlink()
            else:
                return {
                    "status": "error",
                    "message": (
                        f"Output file '{output_file}' already exists. "
                        "Enable 'Overwrite if exists' to replace it."
                    ),
                }

        logger.info(
            "Converting %s -> %s using database: %s", input_file, output_file, database_file
        )
        stats = convert_can_trace(
            input_file,
            database_file,
            output_file,
            emit_schemas_on_init=emit_all_schemas,
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
    from .cli.export import export_to_candump

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
                "message": f"Output file cannot be the same as input file: {input_file}",
            }

        if output_file.exists():
            if overwrite:
                logger.info("Removing existing file: %s", output_file)
                output_file.unlink()
            else:
                return {
                    "status": "error",
                    "message": (
                        f"Output file '{output_file}' already exists. "
                        "Enable 'Overwrite if exists' to replace it."
                    ),
                }

        logger.info("Exporting %s -> %s", input_file, output_file)
        stats = export_to_candump(input_file, output_file)
        if stats["frame_count"] == 0:
            return {
                "status": "warning",
                "message": (
                    "No raw CAN frames found in trace. "
                    "Ensure 'Log Raw CAN Frames' was enabled when recording."
                ),
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


# ─── Registration helper ────────────────────────────────────────────────────


def register_actions(registry: ActionsRegistry) -> list[str]:
    """Register every @action-decorated free function in this module by its
    bare function name. The leading `can/` segment that consumers see comes
    from `zelos_sdk.init(name="can", actions=True)` — the service-name prefix
    is concatenated at serve time, so registering the raw `__name__` here
    produces the desired `can/<func_name>` wire paths.

    Returns the list of registered names (without the service prefix)."""
    module = sys.modules[__name__]
    registered: list[str] = []
    for name, obj in inspect.getmembers(module):
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) and hasattr(obj, "_action"):
            registry.register(obj, name=name)
            registered.append(name)
    logger.info("Registered %d CAN actions", len(registered))
    return registered
