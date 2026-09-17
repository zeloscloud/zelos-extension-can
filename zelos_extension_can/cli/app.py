"""App-based configuration mode for CAN tracing."""

import asyncio
import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import can.exceptions
import zelos_sdk
from zelos_sdk.extensions import load_config
from zelos_sdk.hooks.logging import TraceLoggingHandler

from .. import ACTION_PREFIX
from .. import actions as can_actions
from ..codec import CanCodec
from .utils import setup_shutdown_handler

logger = logging.getLogger(__name__)

#: `advanced` settings and their defaults. Everything here is global: one value
#: applies to every bus. The four bus-level keys were per-bus before and are
#: still honoured per-bus (see `_prepare_bus_config`) so old configs keep their
#: settings, but they are no longer offered in the schema.
ADVANCED_DEFAULTS: dict = {
    "prefix": "CAN",
    "log_raw_frames": True,
    "receive_own_messages": True,
    "emit_schemas_on_init": False,
    "timestamp_mode": "auto",
    "log_level": "INFO",
}

#: Advanced keys the codec reads off a bus config, so a legacy per-bus value
#: still wins over the global one.
_BUS_LEVEL_ADVANCED = (
    "log_raw_frames",
    "receive_own_messages",
    "emit_schemas_on_init",
    "timestamp_mode",
)

#: Trace source names are an allow-list: letters, digits, space, `_`, `-`.
#: `/` is a catalog path separator and would silently re-nest the whole tree.
_SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]+$")


def resolve_advanced(config: dict) -> dict:
    """Merge the `advanced` object over its defaults.

    A top-level `log_level` from a pre-`advanced` config is still honoured.
    """
    supplied = config.get("advanced") or {}
    advanced = {**ADVANCED_DEFAULTS, **supplied}
    if "log_level" not in supplied and config.get("log_level"):
        advanced["log_level"] = config["log_level"]
    return advanced


def trace_log_handler(shared_source: Any) -> logging.Handler:
    """Handler that mirrors extension logs into the trace.

    With a prefix, logs ride the shared source and read `<prefix>/log`;
    with it cleared they get their own `can_log` source, as before.
    """
    handler = TraceLoggingHandler(shared_source if shared_source is not None else "can_log")
    handler.setLevel(logging.INFO)
    return handler


def _resolve_prefix(advanced: dict) -> str:
    """Validate the configured prefix as a trace source name. Empty clears it."""
    prefix = str(advanced.get("prefix") or "").strip()
    if prefix and not _SOURCE_NAME_RE.match(prefix):
        logger.error(
            "Invalid Prefix %r: use letters, digits, space, '_' or '-' only (no '/'), "
            "or clear it to keep one trace source per bus.",
            prefix,
        )
        sys.exit(1)
    return prefix


def _prepare_bus_config(
    bus_config: dict, demo_dbc_path: Path, advanced: dict | None = None
) -> dict:
    """Prepare a bus configuration, handling demo and 'other' interface modes.

    Folds a legacy single `database_file` into the `database_files` list and
    applies the global `advanced` settings that used to live per-bus.

    :param bus_config: Raw bus configuration from the buses array
    :param demo_dbc_path: Path to demo DBC file
    :param advanced: Resolved advanced settings (defaults applied when omitted)
    :return: Prepared configuration dict
    """
    config = bus_config.copy()
    bus_name = config.get("name", "bus")
    advanced = advanced if advanced is not None else dict(ADVANCED_DEFAULTS)

    # A pre-list config carries one `database_file`; it takes precedence in the
    # merge, so prepend it and drop the old key.
    legacy = config.pop("database_file", None)
    files = list(config.get("database_files") or [])
    if legacy:
        files = [legacy, *files]
    config["database_files"] = files

    # Global advanced settings; a legacy per-bus value overrides.
    for key in _BUS_LEVEL_ADVANCED:
        config.setdefault(key, advanced[key])

    # Handle demo interface selection
    if config.get("interface") == "demo":
        logger.info(f"[{bus_name}] Demo mode: using built-in EV simulator")
        config["demo_mode"] = True
        config["interface"] = "virtual"
        config["channel"] = "vcan0"
        config["database_files"] = [str(demo_dbc_path)]
        config["receive_own_messages"] = True

    # Handle "other" interface - merge config_json into main config
    if config.get("interface") == "other":
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

    # Handle ssh-socketcan - synthesize the "[user@]host:iface" channel the
    # transport parses from the user-facing remote_host / ssh_user /
    # remote_channel fields.
    if config.get("interface") == "ssh-socketcan":
        host = config.get("remote_host")
        if not host:
            logger.error(f"[{bus_name}] 'ssh-socketcan' interface requires 'remote_host'")
            sys.exit(1)
        user = config.get("ssh_user")
        iface = config.get("remote_channel", "can0")
        config["channel"] = f"{user}@{host}:{iface}" if user else f"{host}:{iface}"
        logger.info(f"[{bus_name}] ssh-socketcan channel: {config['channel']}")

    return config


def _create_codecs(
    config: dict,
    demo_dbc_path: Path,
    advanced: dict | None = None,
    source: Any = None,
) -> list[tuple[CanCodec, str]]:
    """Create CanCodec instances for all buses in the configuration.

    :param config: Full configuration dict with 'buses' array
    :param demo_dbc_path: Path to demo DBC file
    :param advanced: Resolved advanced settings
    :param source: Shared TraceSource when a prefix is configured, else None
    :return: List of (codec, action_registry_name) tuples
    """
    codecs: list[tuple[CanCodec, str]] = []

    buses = config.get("buses", [])
    if not buses:
        logger.error("No buses configured. Add at least one bus to the 'buses' array.")
        sys.exit(1)

    advanced = advanced if advanced is not None else dict(ADVANCED_DEFAULTS)

    # Prepare all configs first to get channel names
    prepared_configs = [_prepare_bus_config(bus, demo_dbc_path, advanced) for bus in buses]

    seen_names: set[str] = set()

    for i, (bus_config, prepared_config) in enumerate(zip(buses, prepared_configs, strict=True)):
        bus_name = (bus_config.get("name") or "").strip()
        if not bus_name:
            # No explicit name: derive from the channel. Channels can contain
            # '.', '@', ':' (ssh-socketcan's "user@host:iface"), which are
            # catalog PATH SEPARATORS in Zelos trace names and would break
            # catalog / `latest` lookups. Sanitize them to '_'.
            bus_name = re.sub(r"[.@:]", "_", prepared_config.get("channel", f"bus{i}"))

        if bus_name in seen_names:
            logger.error(f"Duplicate bus name '{bus_name}'. Each bus must have a unique name.")
            sys.exit(1)
        seen_names.add(bus_name)

        codec = CanCodec(prepared_config, bus_name=bus_name, source=source)
        codecs.append((codec, bus_name))

        logger.info(
            f"Created bus codec: {bus_name} "
            f"({prepared_config['interface']}:{prepared_config.get('channel', 'N/A')})"
        )

    return codecs


async def _run_codecs_async(codecs: list[CanCodec]) -> None:
    """Run multiple codecs concurrently.

    :param codecs: List of CanCodec instances to run
    """
    # Start buses inside the try so that if one start() raises, the finally
    # stops the buses already started. Otherwise an already-started bus owning
    # non-daemon ssh threads (with a live remote candump session) is never torn
    # down and the process hangs forever.
    started: list[CanCodec] = []
    try:
        for codec in codecs:
            codec.start()
            started.append(codec)

        # Run all codecs concurrently using their async run method
        tasks = [asyncio.create_task(codec._run_async()) for codec in codecs]
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Codec tasks cancelled")
    finally:
        # Ensure every bus we started is stopped (in reverse start order).
        for codec in reversed(started):
            codec.stop()


def run_app_mode(demo: bool, file: Path | None, demo_dbc_path: Path) -> None:
    """Run CAN extension in app-based configuration mode.

    :param demo: Enable demo mode (adds a demo bus if no buses configured)
    :param file: Optional output file for trace recording
    :param demo_dbc_path: Path to demo DBC file
    """
    # Load and validate configuration
    config = load_config()
    advanced = resolve_advanced(config)
    prefix = _resolve_prefix(advanced)

    # Apply log level from config (global setting)
    log_level_str = advanced["log_level"]
    try:
        log_level = getattr(logging, log_level_str)
        logging.getLogger().setLevel(log_level)
        logger.info(f"Log level set to: {log_level_str}")
    except AttributeError:
        logger.warning(f"Invalid log level '{log_level_str}', using INFO")
        logging.getLogger().setLevel(logging.INFO)

    # If demo flag is set and no buses configured, add a demo bus
    if demo:
        logger.info("Demo mode enabled via --demo flag")
        if not config.get("buses"):
            config["buses"] = [{"name": "demo", "interface": "demo"}]
        else:
            # Add demo bus to existing buses
            config["buses"].append({"name": "demo", "interface": "demo"})

    # Determine output file if --file was specified
    output_file = None
    if file is not None:
        # If --file was given without a value, use UTC timestamp
        if str(file) == ".":
            utc_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            file = Path(f"{utc_timestamp}.trz")
        output_file = file
        logger.info(f"Recording trace to: {output_file}")

    # One shared trace source when a prefix is configured, so every bus's
    # events nest under it as `<prefix>/<bus>/<event>`. It is created BEFORE the
    # codecs (which need it) and before `zelos_sdk.init` below; the default
    # prefix is the action prefix, so this is the same global source `init`
    # would make — `init_global_source` is idempotent and returns it again.
    shared_source = None
    if prefix:
        shared_source = (
            zelos_sdk.init_global_source(ACTION_PREFIX)
            if prefix == ACTION_PREFIX
            else zelos_sdk.TraceSource(prefix)
        )
        logger.info("Trace prefix: %s", prefix)
    else:
        logger.info("Trace prefix cleared: one trace source per bus")

    # Create all CAN codecs from buses array
    codec_pairs = _create_codecs(config, demo_dbc_path, advanced, shared_source)
    codecs = [codec for codec, _ in codec_pairs]

    # Populate the shared codec registry that `actions.py` reads from. The
    # action surface is a single global namespace — `CAN/send_message`,
    # `CAN/get_tx_state`, etc. — with a `codec` parameter that selects which
    # bus to operate on. CLI usage:
    #
    #   zelos actions execute CAN/send_raw \
    #       --params '{"codec":"busA","can_id":"0x100","data":"01 02"}'
    #
    # Web apps discover the bus list by calling `CAN/list_codecs`.
    # Codec-name uniqueness is already enforced inside _create_codecs (multi-bus
    # path) and trivially satisfied in the single-bus path.
    for codec, codec_name in codec_pairs:
        can_actions.CAN_CODECS[codec_name] = codec

    # Register the actions module once. The address prefix is supplied by
    # `init(name=ACTION_PREFIX, actions=True)` below.
    can_actions.register_actions(zelos_sdk.actions_registry)

    # Initialize SDK. `ACTION_PREFIX` is package-level so the live namespace and
    # the packaged at-rest inventory cannot drift apart.
    zelos_sdk.init(name=ACTION_PREFIX, log_level="info", actions=True)

    logging.getLogger().addHandler(trace_log_handler(shared_source))

    # Setup shutdown handler for all codecs.
    for codec in codecs:
        setup_shutdown_handler(codec)

    # Log startup info
    bus_count = len(codecs)
    logger.info(f"Starting CAN extension with {bus_count} bus{'es' if bus_count > 1 else ''}")

    # Run with optional trace writer. A bus that can't start (bad interface,
    # unreachable / unauthenticated ssh host, missing remote can-utils, ...)
    # raises can.exceptions.CanError — CanInitializationError and
    # CanInterfaceNotImplementedError are subclasses. Catch it and exit cleanly
    # with a one-line reason instead of dumping a raw traceback that looks like
    # a crash. _run_codecs_async's try/finally has already stopped any bus that
    # DID start before the failing one, so cleanup is complete by the time we
    # get here.
    try:
        if output_file:
            with zelos_sdk.TraceWriter(str(output_file)):
                asyncio.run(_run_codecs_async(codecs))
        else:
            asyncio.run(_run_codecs_async(codecs))
    except can.exceptions.CanError as e:
        logger.error("CAN bus failed to start: %s", e)
        sys.exit(1)
