"""Free-floating CAN action functions registered under `<ACTION_PREFIX>/<name>`.

This is the standard pattern for multi-bus extensions: a single global namespace
keyed by a `codec` parameter, not per-bus action paths (`<prefix>/<bus>/<action>`).

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
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zelos_sdk.actions import ActionsRegistry, action

from .utils.file_utils import resolve_database_file

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


def _as_paths(value: str | list[str]) -> list[Path]:
    """Normalize a DBC action parameter: one path or a list, empties dropped."""
    values = [value] if isinstance(value, str) else list(value)
    return [Path(v) for v in values if v]


def _clear_destination(destination: Path, overwrite: bool) -> None:
    """Make `destination` writable, or refuse.

    `TraceWriter` will not open a path that already exists, so replacing one
    means removing it first.
    """
    if not destination.exists():
        return
    if not overwrite:
        raise FileExistsError(f"Output exists: {destination} (enable Overwrite to replace it)")
    destination.unlink()


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
    "DBC message summary list for a bus — identifiers only, no per-signal "
    "metadata. One entry per definition, keyed by `key` (`<id hex>_<Name>`, the "
    "trace event name), which is what the transmit actions address. Use "
    "describe_message to fetch a specific message's full signal detail on "
    "demand.",
)
@action.select("codec", title="CAN bus", choices=_available_codecs)
def list_messages(codec: str) -> dict[str, Any]:
    return _get_codec(codec).list_messages()


@action(
    "Describe Message",
    "Full signal-level detail for a single DBC message (units, ranges, "
    "value tables, mux structure). "
    "`message` is a key from list_messages, or a name only one "
    "definition carries.",
)
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("message", title="DBC message key or name")
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


@action(
    "Send Message",
    "Send a one-shot DBC-encoded message. "
    "`message` is a key from list_messages, or a name only one "
    "definition carries.",
)
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("message", title="DBC message key or name")
@action.text("signals_json", title="Signals (JSON object)", placeholder='{"Speed": 50}')
@action.text("mux", title="Multiplexer (optional)", required=False, default="")
def send_message(codec: str, message: str, signals_json: str, mux: str = "") -> dict[str, Any]:
    return _get_codec(codec).send_message(message, signals_json, mux)


@action(
    "Encode Preview",
    "Encode a DBC message without transmitting. Returns the bytes that "
    "send_message would emit. "
    "`message` is a key from list_messages, or a name only one "
    "definition carries.",
)
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("message", title="DBC message key or name")
@action.text("signals_json", title="Signals (JSON object)", placeholder='{"Speed": 50}')
@action.text("mux", title="Multiplexer (optional)", required=False, default="")
def encode_preview(codec: str, message: str, signals_json: str, mux: str = "") -> dict[str, Any]:
    return _get_codec(codec).encode_preview(message, signals_json, mux)


@action(
    "Start Periodic Message",
    "Start DBC-encoded periodic transmission. Returns {task_id, replaced}. "
    "`message` is a key from list_messages, or a name only one "
    "definition carries.",
)
@action.select("codec", title="CAN bus", choices=_available_codecs)
@action.text("message", title="DBC message key or name")
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
# The DBC source is explicit via `database_path`; a `codec` lends its loaded
# list when that is empty. We don't silently default to "first registered
# codec" because that couples a file conversion to whichever bus started first.
# With neither, the conversion writes raw frames only, like the CLI.


@action(
    "Convert Trace File",
    "Convert a CAN log (.asc / .blf / .trc / candump .log) to Zelos trace "
    "format (.trz). Provide `database_path` (one path or several) OR a `codec` "
    "whose loaded DBCs will be used; with neither, only raw frames are written.",
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
    description=(
        "Database file, or a list of them in precedence order. If empty, `codec` "
        "lends its list; with neither, only raw frames are written."
    ),
    placeholder="/path/to/file.dbc",
    widget="file-picker",
)
@action.select(
    "codec",
    required=False,
    default="",
    title="Codec (fallback DBC source)",
    description=(
        "Used only when `database_path` is empty — the named codec's DBCs drive the conversion."
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
    database_path: str | list[str] = "",
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
        supplied = _as_paths(database_path)
        if supplied:
            database_files = [resolve_database_file(p).resolve() for p in supplied]
            logger.info("Using user-specified databases: %s", database_files)
        elif codec:
            # _get_codec raises ValueError on unknown codec — propagated
            # verbatim by the pass-through handler below.
            database_files = list(_get_codec(codec).database_files)
            logger.info("Using codec '%s' databases: %s", codec, database_files)
        else:
            database_files = []
            logger.info("No database given: writing raw frames only")

        input_file = Path(input_path).expanduser().resolve()
        if not input_file.exists():
            raise FileNotFoundError(f"Input file not found: {input_file}")

        if not output_path:
            output_path = str(input_file.with_suffix(".trz"))
        output_file = Path(output_path).expanduser().resolve()
        if output_file.suffix.lower() != ".trz":
            output_file = output_file.with_suffix(".trz")

        if output_file == input_file:
            raise ValueError(f"Output file cannot be the same as input file: {input_file}")

        _clear_destination(output_file, overwrite)

        logger.info(
            "Converting %s -> %s using databases: %s", input_file, output_file, database_files
        )
        stats = convert_can_trace(
            input_file,
            database_files,
            output_file,
            emit_schemas_on_init=emit_all_schemas,
        )
        return {
            "status": "success",
            "input_file": str(input_file),
            "database_file": str(database_files[0]) if database_files else None,
            "database_files": [str(p) for p in database_files],
            "output_file": str(output_file),
            **stats.to_dict(),
        }
    except (FileNotFoundError, FileExistsError, ValueError):
        # Already self-describing (validation above, plus convert_can_trace's
        # own path/format errors) — propagate verbatim.
        raise
    except ImportError as e:
        raise ImportError(f"Missing dependency: {e}") from e
    except Exception as e:
        logger.exception("Conversion failed")
        raise RuntimeError(f"Conversion failed: {e}") from e


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
            raise FileNotFoundError(f"Input file not found: {input_file}")
        if input_file.suffix.lower() != ".trz":
            raise ValueError(f"Input file must be a .trz file: {input_file}")

        if not output_path:
            output_path = str(input_file.with_suffix(".log"))
        output_file = Path(output_path).expanduser().resolve()
        if output_file.suffix.lower() != ".log":
            output_file = output_file.with_suffix(".log")

        if output_file == input_file:
            raise ValueError(f"Output file cannot be the same as input file: {input_file}")

        _clear_destination(output_file, overwrite)

        logger.info("Exporting %s -> %s", input_file, output_file)
        stats = export_to_candump(input_file, output_file)
        if stats["frame_count"] == 0:
            # Raise, do not return. `export_to_candump` writes no file when the
            # trace has no raw sources, so a returned payload here reads as
            # success (a plain return means "no verdict", which the wire maps to
            # DONE) while `output_file` does not exist. A caller chaining on exit
            # status would proceed against a missing file.
            raise ValueError(
                "No raw CAN frames found in trace "
                f"(sources found: {stats['sources_found']}). "
                "Ensure 'Log Raw CAN Frames' was enabled when recording."
            )
        return {
            "status": "success",
            "input_file": str(input_file),
            "output_file": str(output_file),
            "frame_count": stats["frame_count"],
            "sources_exported": stats["sources_exported"],
        }
    except (FileNotFoundError, FileExistsError, ValueError):
        # Already self-describing — propagate verbatim.
        raise
    except Exception as e:
        logger.exception("Export failed")
        raise RuntimeError(f"Export failed: {e}") from e


# ─── Config-form hooks (standalone: the form calls these before a first start) ─
#
# Both answer the app's schema hooks in `config.schema.json`: the root
# `ui:options.autoconfig` button, and the `action-choices` widget on a
# socketcan channel. They read sysfs only — no privileges, no python-can, and
# nothing that needs the extension to be running.

#: Where Linux publishes its network interfaces. A CAN interface is a netdev
#: like any other, told apart by its ARPHRD type.
_SYS_CLASS_NET = Path("/sys/class/net")
_ARPHRD_CAN = "280"


def _read_sysfs(path: Path) -> str:
    """One sysfs attribute, or "" when it is absent or unreadable."""
    try:
        return path.read_text().strip()
    except OSError:  # raced away mid-scan, or not readable — just unknown
        return ""


def _is_virtual(name: str) -> bool:
    """A kernel vcan interface: usable, but with no CAN hardware behind it."""
    return name.startswith("vcan")


def _interface_rank(iface: dict[str, str]) -> tuple[int, str]:
    """Real CAN devices first, virtual ones after; names break ties."""
    return (1 if _is_virtual(iface["name"]) else 0, iface["name"])


def _interface_choice(iface: dict[str, str]) -> dict[str, str]:
    """One `choices` entry: the name, and what a person needs to pick it.

    The app's picker renders `detail` as dim right-aligned text beside the
    value, so `detail` carries the notes alone: `up, gs_usb` / `virtual`.

    `unknown` is left out: vcan reports it and is perfectly usable, so it says
    nothing. `down` is kept — that bus needs `ip link set <if> up` first.
    """
    notes = [iface["state"]] if iface["state"] in ("up", "down") else []
    kind = iface["driver"] or ("virtual" if _is_virtual(iface["name"]) else "")
    if kind:
        notes.append(kind)
    return {"value": iface["name"], "detail": ", ".join(notes)}


def _local_can_interfaces() -> list[dict[str, str]]:
    """This machine's SocketCAN interfaces: name, operstate, driver.

    SocketCAN is Linux-only, so macOS/Windows answer with an honest empty list
    rather than an error — there is nothing to enumerate there.
    """
    if sys.platform != "linux" or not _SYS_CLASS_NET.is_dir():
        return []
    found = []
    for entry in _SYS_CLASS_NET.iterdir():
        if _read_sysfs(entry / "type") != _ARPHRD_CAN:
            continue
        # A symlink into the driver owning the device (gs_usb, peak_usb, ...),
        # absent for a virtual interface, which has no device behind it.
        driver = entry / "device" / "driver"
        found.append(
            {
                "name": entry.name,
                "state": _read_sysfs(entry / "operstate") or "unknown",
                "driver": driver.resolve().name if driver.exists() else "",
            }
        )
    return sorted(found, key=_interface_rank)


@action(
    "List CAN Interfaces",
    "SocketCAN interfaces on the machine running the agent, as choices for a "
    "bus's Channel field, which also accepts a name typed by hand. Empty on "
    "macOS/Windows, which have no SocketCAN.",
    # Reading sysfs opens no socket and needs no privileges, and the config form
    # wants the list before the extension has ever run.
    standalone=True,
)
def list_interfaces() -> dict[str, Any]:
    """The app's `action-choices` contract: `choices` in the order to show."""
    return {
        "status": "success",
        "choices": [_interface_choice(iface) for iface in _local_can_interfaces()],
    }


@action(
    "Auto-configure",
    "One zelos-socketcan bus per SocketCAN interface on the machine running the "
    "agent, for the config form's Auto-configure button. Review it, then save "
    "and start.",
    standalone=True,
)
def auto_config() -> dict[str, Any]:
    """The app's auto-configure contract: the keys of `config` replace the form's.

    Only `buses` is returned, so whatever is set under Advanced survives. Never
    an ssh-socketcan bus: there is no remote host to guess.
    """
    interfaces = _local_can_interfaces()
    if not interfaces:
        return {
            "status": "error",
            "message": (
                "No SocketCAN interface on this machine. Add an ssh-socketcan bus for a "
                "remote device, or a pcan/kvaser/vector bus."
            ),
        }
    # zelos-socketcan, not socketcan: the Rust bus is the native local path.
    # No `name`, so each bus is named after its channel. A down interface is
    # configured as it is, with no note: the button surfaces only an error
    # message, and the Channel picker already labels it `down`.
    return {
        "status": "success",
        "config": {
            "buses": [
                {"interface": "zelos-socketcan", "channel": iface["name"], "database_files": []}
                for iface in interfaces
            ]
        },
    }


# ─── Standalone (runs with the extension stopped) ───────────────────────────


def _configured_database_files() -> list[str]:
    """The first configured bus's database list, if any.

    Config is at-rest state — it is written on Start and persists across stop —
    so this resolves whether or not the extension is running. It is applied in
    the action body rather than as a schema default because the inventory is
    dumped at package time, before any config exists.
    """
    from .codec import bus_database_files  # deferred: pulls in can/cantools

    try:
        from zelos_sdk.extensions.config import load_config

        buses = (load_config() or {}).get("buses") or []
    except Exception:  # no config yet, or schema mismatch — not an error here
        return []
    for bus in buses:
        if isinstance(bus, dict) and (files := bus_database_files(bus)):
            return files
    return []


def _open_in_app(path: Path) -> None:
    """Hand a finished .trz to the desktop app via the OS file association.

    There is no agent RPC for "open this trace", so the route is the platform
    opener plus the app's own `.trz` association.

    On Windows that is `os.startfile` (ShellExecuteW): the path is one argument
    to one API call with no shell in the way. `cmd /c start` would re-parse the
    command line, and `list2cmdline` quotes only for whitespace and quotes — so
    a space-free caller-supplied path containing `&` or `%VAR%` would select a
    command. ShellExecuteW also does not give us a child process, so the two
    POSIX details below do not apply to it.

    On POSIX two details are load-bearing when this runs at rest:

    - **Own session.** A standalone action runs in a `setsid`-detached one-shot
      whose *process group* the supervisor kills on any abnormal exit. A child
      in that group would be killed with it, so the opener gets its own session.
    - **Detached stdio.** The supervisor drains the run's stdout/stderr pipes and
      waits for them to close. A child inheriting them holds them open after the
      action returns, which stalls the run and then trips the "pipes open but the
      child is gone" terminate path. Redirect to devnull so the run ends cleanly.

    Raises whatever the opener raises; the caller decides that a conversion which
    produced a file is not a failure just because the GUI did not come up.
    """
    if sys.platform == "win32":
        os.startfile(path)  # ShellExecuteW: one path argument, no shell to re-parse it
        return

    argv = ["open", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path)]
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@action(
    "Convert CAN Log",
    "Convert a CAN log file to a Zelos trace (.trz). Runs without the extension "
    "running — no bus, no live connection.",
    # Conversion is I/O bound over files that can reach multi-GB. 30 minutes is
    # a ceiling for the pathological case, not an expectation; the action
    # returns as soon as the file is written. Note the AI tool bridge clamps
    # its own calls to MAX_ACTION_TOOL_TIMEOUT_MS (5 min) regardless, so long
    # conversions are an action-panel / CLI path.
    timeout=1800.0,
    standalone=True,
)
@action.text(
    "input_file",
    title="CAN log",
    description="Source .asc, .blf, .trc, .log, .csv or .mf4",
    widget="file_path_picker",
)
@action.text(
    "database_file",
    title="Database (.dbc)",
    description=(
        "One path or several, in precedence order. Defaults to the databases "
        "configured for this extension's first bus; with none, only raw frames "
        "are written."
    ),
    required=False,
    default="",
    widget="file_path_picker",
)
@action.text(
    "output_file",
    title="Output (.trz)",
    description="Defaults to the input file with a .trz suffix",
    required=False,
    default="",
    widget="file_path_picker",
)
@action.boolean(
    "force", title="Overwrite existing output", required=False, default=False, widget="toggle"
)
@action.boolean(
    "open_on_complete",
    title="Open trace when finished",
    description="Open the converted .trz in the Zelos app once the conversion succeeds",
    required=False,
    default=False,
    widget="toggle",
)
def convert(
    input_file: str,
    database_file: str | list[str] = "",
    output_file: str = "",
    force: bool = False,
    open_on_complete: bool = False,
) -> dict[str, Any]:
    """Convert a CAN log to .trz. Shares `convert_can_trace` with the `convert`
    CLI command, so the two surfaces cannot diverge."""
    from .converter import SUPPORTED_FORMATS, convert_can_trace

    source = Path(input_file).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"Input file not found: {source}")
    if source.suffix.lower() not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported format: {source.suffix}. Supported: {', '.join(SUPPORTED_FORMATS.keys())}"
        )

    supplied = _as_paths(database_file)
    database_paths = [resolve_database_file(p) for p in supplied or _configured_database_files()]

    # Resolved, not just expanded. Two reasons: a relative path would otherwise
    # resolve against the extension's working directory rather than the caller's,
    # and an unresolved path starting with `-` reaches the platform opener as a
    # flag (`open -a.trz` parses as `open -a <app>`).
    destination = (
        Path(output_file).expanduser().resolve() if output_file else source.with_suffix(".trz")
    )
    _clear_destination(destination, force)

    stats = convert_can_trace(source, database_paths, destination)

    # The trace exists on disk from here on. Failing to open it is a worse
    # outcome to report than it is a real one: the conversion succeeded, and
    # raising now would tell the caller the whole run failed and invite a
    # re-run of work already done. Report it in-band instead.
    opened = False
    open_error: str | None = None
    if open_on_complete:
        try:
            _open_in_app(destination)
            opened = True
        except Exception as e:  # noqa: BLE001 — any spawn failure is non-fatal here
            open_error = str(e)
            logger.warning("Converted %s but could not open it: %s", destination, e)

    result = {
        "status": "success",
        "input_file": str(source),
        "database_file": str(database_paths[0]) if database_paths else None,
        "database_files": [str(p) for p in database_paths],
        "output_file": str(destination),
        "opened": opened,
        **stats.to_dict(),
    }
    if open_error is not None:
        result["open_error"] = open_error
    return result


# ─── Registration helper ────────────────────────────────────────────────────


def register_actions(registry: ActionsRegistry) -> list[str]:
    """Register every @action-decorated free function in this module by its
    bare function name. The leading `CAN/` segment that consumers see comes
    from `zelos_sdk.init(name=ACTION_PREFIX, actions=True)` — the service-name
    prefix is concatenated at serve time, so registering the raw `__name__`
    here produces the desired `CAN/<func_name>` wire paths.

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
