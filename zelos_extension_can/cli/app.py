"""App-based configuration mode for CAN tracing."""

import asyncio
import contextlib
import logging
import sys
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import can.exceptions
import zelos_sdk
from zelos_can.bus import BUS_DEFAULTS, BusConfigError, prepare_bus_config
from zelos_can.codec import CanCodec
from zelos_can.naming import DEFAULT_PREFIX, LOG_SOURCE_NAME, name_error, trace_layout
from zelos_sdk.extensions import load_config
from zelos_sdk.hooks.logging import TraceLoggingHandler

from .. import ACTION_PREFIX, INTERFACES, gs_usb
from .. import actions as can_actions
from .utils import setup_shutdown_handler

logger = logging.getLogger(__name__)

#: `advanced` settings and their defaults. Everything here is global: one value
#: applies to every bus. The bus-level keys (`BUS_DEFAULTS`) are copied onto
#: each bus config (see `prepare_bus_config`); a bus that still sets one itself
#: keeps it.
ADVANCED_DEFAULTS: dict = {
    "prefix": DEFAULT_PREFIX,
    **BUS_DEFAULTS,
    "j1939": False,
    "j1939_node": "",
    "canopen": False,
    "log_level": "INFO",
}

#: Bus-level keys this extension owns (not zelos-can's `BUS_DEFAULTS`), copied
#: onto each bus config the same way.
EXTENSION_BUS_KEYS = ("j1939", "canopen")

#: Interfaces whose bus runs zelos-can's Rust codec, the only ones with a J1939 node.
RUST_INTERFACES = ("zelos-socketcan", "zelos-ssh-socketcan")


def resolve_advanced(config: dict) -> dict:
    """Merge the `advanced` object over its defaults.

    An absent `prefix` takes the default; a present-but-empty one clears it.
    A top-level `log_level` from a pre-`advanced` config is still honoured.
    """
    supplied = config.get("advanced") or {}
    advanced = {**ADVANCED_DEFAULTS, **supplied}
    if "log_level" not in supplied and config.get("log_level"):
        advanced["log_level"] = config["log_level"]
    return advanced


def _validate_name(value: str, label: str, reserved: Collection[str] = ()) -> None:
    """Exit with a one-line reason unless `value` is a legal trace name."""
    error = name_error(value, label, reserved)
    if error:
        logger.error("%s", error)
        sys.exit(1)


def _prepare_bus_config(
    bus_config: dict, demo_dbc_path: Path, advanced: dict | None = None
) -> dict:
    """`prepare_bus_config`, exiting with its one-line reason on bad input."""
    try:
        return prepare_bus_config(bus_config, demo_dbc_path, advanced)
    except BusConfigError as e:
        logger.error("%s", e)
        sys.exit(1)


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
    # The configured interface is a label; the bus opens its python-can name.
    prepared_configs = []
    for bus in buses:
        bus = {**bus, "interface": INTERFACES[bus["interface"]]}
        if bus["interface"] == gs_usb.INTERFACE:
            gs_usb.require_extra()
            bus = gs_usb.bus_config(bus)
        prepared_configs.append(_prepare_bus_config(bus, demo_dbc_path, advanced))
    # Also covers gs_usb through "Other (python-can)".
    if any(p["interface"] == gs_usb.INTERFACE for p in prepared_configs):
        gs_usb.prime_libusb()
    for prepared in prepared_configs:
        for key in EXTENSION_BUS_KEYS:
            prepared.setdefault(key, advanced[key])
        if advanced["j1939_node"] and prepared["interface"] in RUST_INTERFACES:
            prepared.setdefault("j1939_node", advanced["j1939_node"])

    seen_names: set[str] = set()

    for i, prepared_config in enumerate(prepared_configs):
        bus_name = (prepared_config.get("name") or "").strip()
        if bus_name:
            # With a prefix the name becomes an event segment, without one a
            # source name; either way it must already be a legal trace name.
            _validate_name(bus_name, "bus Name", (LOG_SOURCE_NAME,))
        else:
            # No explicit name: derive from the channel, for ssh-socketcan the
            # remote interface alone (not "user@host:iface"). Channels can
            # contain '.', which is a catalog PATH SEPARATOR in Zelos trace
            # names and would break catalog / `latest` lookups.
            channel = prepared_config.get("channel", f"bus{i}")
            if prepared_config.get("interface") == "zelos-ssh-socketcan":
                channel = channel.rpartition(":")[2]
            bus_name = zelos_sdk.sanitize_name(channel, kind="source")

        if bus_name in seen_names:
            logger.error(
                f"Duplicate bus name '{bus_name}'. Each bus must have a unique name; "
                "set Name on one of them."
            )
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
    prefix = str(advanced.get("prefix") or "").strip()
    _validate_name(prefix, "Prefix")

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
            config["buses"] = [{"name": "demo", "interface": "Demo"}]
        else:
            # Add demo bus to existing buses
            config["buses"].append({"name": "demo", "interface": "Demo"})

    # Determine output file if --file was specified
    output_file = None
    if file is not None:
        # If --file was given without a value, use UTC timestamp
        if str(file) == ".":
            utc_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            file = Path(f"{utc_timestamp}.trz")
        output_file = file
        logger.info(f"Recording trace to: {output_file}")

    # Register the actions module once. `choices=` providers are evaluated at
    # form-render time, so this does not need the codecs yet, and the address
    # prefix is supplied by `init(name=ACTION_PREFIX, actions=True)` below.
    can_actions.register_actions(zelos_sdk.actions_registry)

    # The trace source and the log handler come up BEFORE the codecs, so the
    # DBC merge summary and any conflict warnings the codecs log land in the
    # trace. `init_global_source` is idempotent and ignores the name on later
    # calls, so `init` below returns this same source rather than making an
    # empty second one. Same layout rule as every other entry point: a prefix
    # means one source for every bus; cleared, each bus owns its own and the
    # logs get theirs.
    log_source_name, _, _ = trace_layout(prefix, LOG_SOURCE_NAME)
    global_source = zelos_sdk.init_global_source(log_source_name)
    shared_source = global_source if prefix else None
    if prefix:
        logger.info("Trace prefix: %s", prefix)
    else:
        logger.info("Trace prefix cleared: one trace source per bus")

    # Initialize SDK. `ACTION_PREFIX` is package-level so the live namespace and
    # the packaged at-rest inventory cannot drift apart.
    zelos_sdk.init(name=ACTION_PREFIX, log_level="info", actions=True)

    handler = TraceLoggingHandler(global_source)
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)

    # A recording opens before the codecs, so it captures startup: the DBC merge
    # summary and any conflict warnings land in `<prefix>/log` too.
    with contextlib.ExitStack() as stack:
        if output_file:
            stack.enter_context(zelos_sdk.TraceWriter(str(output_file)))

        # Create all CAN codecs from buses array. A bad conflict policy, prefix
        # or name raises ValueError, a missing DBC FileNotFoundError, and the
        # Rust loader RuntimeError — all configuration mistakes, so report the
        # reason and stop rather than dumping a traceback that reads like a
        # crash.
        try:
            codec_pairs = _create_codecs(config, demo_dbc_path, advanced, shared_source)
        except (ValueError, FileNotFoundError, RuntimeError) as e:
            logger.error("CAN bus configuration is invalid: %s", e)
            sys.exit(1)
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
        # Codec-name uniqueness is already enforced inside _create_codecs
        # (multi-bus path) and trivially satisfied in the single-bus path.
        for codec, codec_name in codec_pairs:
            can_actions.CAN_CODECS[codec_name] = codec

        setup_shutdown_handler()

        bus_count = len(codecs)
        logger.info(f"Starting CAN extension with {bus_count} bus{'es' if bus_count > 1 else ''}")

        # A bus that can't start (bad interface, unreachable / unauthenticated
        # ssh host, missing remote can-utils, ...) raises can.exceptions.CanError
        # — CanInitializationError and CanInterfaceNotImplementedError are
        # subclasses, as is the mid-run SshPermanentError; a bad `config_json`
        # raises ValueError and the Rust-side loader RuntimeError. Exit cleanly
        # with a one-line reason instead of a traceback that looks like a crash.
        # _run_codecs_async's try/finally has already stopped every bus it
        # started, so cleanup is complete by the time we get here.
        try:
            asyncio.run(_run_codecs_async(codecs))
        except (can.exceptions.CanError, ValueError, FileNotFoundError, RuntimeError) as e:
            logger.error("CAN bus failed: %s", e)
            sys.exit(1)
