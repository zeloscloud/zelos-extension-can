"""App-based configuration mode for CAN tracing."""

import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import can.exceptions
import zelos_sdk
from zelos_can.bus import BUS_DEFAULTS, BusConfigError, prepare_bus_config
from zelos_can.codec import CanCodec
from zelos_can.naming import DEFAULT_PREFIX, LOG_SOURCE_NAME, name_error, trace_layout
from zelos_can.ssh_socketcan import SshPermanentError
from zelos_sdk.extensions import ConfigValidationError, load_config
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
    "j1939_name": "",
    "j1939_address": None,
    "canopen": False,
    "retry_unreachable_at_start": False,
    "log_level": "INFO",
}

#: Bus-level keys this extension owns (not zelos-can's `BUS_DEFAULTS`), copied
#: onto each bus config the same way.
EXTENSION_BUS_KEYS = ("j1939", "canopen")


def _j1939_node(advanced: dict) -> str | dict | None:
    """The node every bus claims as: a DBC node name, or an explicit NAME and
    address. zelos-can parses and validates either."""
    node = (advanced["j1939_node"] or "").strip()
    name, address = (advanced["j1939_name"] or "").strip(), advanced["j1939_address"]
    if not name and address is None:
        return node or None
    if node:
        raise ValueError("set J1939 Node, or J1939 NAME and J1939 Address, not both")
    return {"name": name, "address": address}


#: The bus `--demo` adds to the configured ones.
DEMO_BUS = {"name": "demo", "interface": "Demo"}


def _load_config(demo: bool) -> dict:
    """`load_config()`; with `demo`, the settings check sees the demo bus among the buses."""
    if not demo:
        return load_config()
    saved_path = Path(os.environ.get("ZELOS_CONFIG_PATH") or "config.json")
    saved = json.loads(saved_path.read_text()) if saved_path.exists() else {}
    saved["buses"] = [*(saved.get("buses") or []), DEMO_BUS]
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        config_path.write_text(json.dumps(saved))
        return load_config(config_path=config_path)


#: `retry_unreachable_at_start` backoff: the ssh supervisor's cadence.
_RETRY_START_INTERVAL = 5.0
_RETRY_START_MAX = 60.0


def resolve_advanced(config: dict) -> dict:
    """Merge the `advanced` object over its defaults.

    An absent `prefix` takes the default; a present-but-empty one clears it.
    A top-level `log_level` from a pre-`advanced` config is still honored.
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

    # The configured interface is a label; the bus opens its python-can name.
    prepared_configs = []
    for bus in buses:
        bus = {**bus, "interface": INTERFACES[bus["interface"]]}
        if bus["interface"] == gs_usb.INTERFACE:
            gs_usb.require_extra()
            bus = gs_usb.bus_config(bus)
        prepared_configs.append(_prepare_bus_config(bus, demo_dbc_path, advanced))
    # Also covers gs_usb through "Other (python-can)".
    gs_buses = [p for p in prepared_configs if p["interface"] == gs_usb.INTERFACE]
    if gs_buses:
        gs_usb.prime_libusb()
    for prepared in gs_buses:
        # Host receive time: the adapter's 32-bit us counter wraps every 71.6 min.
        prepared["timestamp_mode"] = "ignore"
    for prepared in prepared_configs:
        for key in EXTENSION_BUS_KEYS:
            prepared.setdefault(key, advanced[key])
        if node := _j1939_node(advanced):
            prepared.setdefault("j1939_node", node)

    seen_names: set[str] = set()

    for i, prepared_config in enumerate(prepared_configs):
        bus_name = (prepared_config.get("name") or "").strip()
        if bus_name:
            # With a prefix the name becomes an event segment, without one a
            # source name; either way it must already be a legal trace name.
            _validate_name(bus_name, "bus Name", (LOG_SOURCE_NAME,))
        else:
            # Sanitized: a channel can hold '.', the path separator of trace names.
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


def _transient_start_error(error: BaseException) -> bool:
    """A start failure a retry can fix: the link is down, not the config or the host.

    A SocketCAN interface that does not exist is a RuntimeError from the Rust
    codec, not a CanError, so it is never retried.
    """
    return isinstance(error, can.exceptions.CanError) and not isinstance(
        error, (SshPermanentError, can.exceptions.CanInterfaceNotImplementedError)
    )


async def _run_codecs_async(codecs: list[CanCodec], retry_unreachable: bool = False) -> None:
    """Run multiple codecs concurrently.

    :param codecs: List of CanCodec instances to run
    :param retry_unreachable: Retry a bus that cannot start instead of stopping
    """
    # Inside the try: a started bus's non-daemon ssh threads would otherwise hang the process.
    started: list[CanCodec] = []
    retries: list[asyncio.Task] = []

    async def retry_then_run(codec: CanCodec, error: Exception) -> None:
        """Retry `codec.start()` with backoff until it succeeds, then run it:
        ERROR once, then once a minute at the backoff cap. A failure retrying
        cannot fix propagates.

        On the loop, not a thread, and registered in `started` before each
        attempt: a start that lands as shutdown begins is still stopped.
        """
        interval = _RETRY_START_INTERVAL
        attempts = 1
        logger.error("[%s] Cannot start: %s; retrying in %.0f s", codec.bus_name, error, interval)
        while True:
            await asyncio.sleep(interval)
            started.append(codec)
            try:
                codec.start()
            except Exception as e:
                started.remove(codec)
                if not _transient_start_error(e):
                    raise
                attempts += 1
                interval = min(interval * 2, _RETRY_START_MAX)
                if interval == _RETRY_START_MAX:
                    logger.error(
                        "[%s] Still cannot start after %d attempts: %s", codec.bus_name, attempts, e
                    )
            else:
                logger.info("[%s] Started after %d attempts", codec.bus_name, attempts)
                break
        await codec._run_async()

    try:
        for codec in codecs:
            try:
                codec.start()
            except Exception as e:
                if not (retry_unreachable and _transient_start_error(e)):
                    raise
                retries.append(asyncio.create_task(retry_then_run(codec, e)))
                continue
            started.append(codec)

        # Run all codecs concurrently using their async run method
        await asyncio.gather(*[codec._run_async() for codec in started], *retries)
    except asyncio.CancelledError:
        logger.info("Codec tasks cancelled")
    finally:
        # A retry whose timer already expired must not start a bus after this.
        for task in retries:
            task.cancel()
        # Ensure every bus we started is stopped (in reverse start order).
        for codec in reversed(started):
            codec.stop()


def run_app_mode(demo: bool, file: Path | None, demo_dbc_path: Path) -> None:
    """Run CAN extension in app-based configuration mode.

    :param demo: Enable demo mode (adds a demo bus to the configured ones)
    :param file: Optional output file for trace recording
    :param demo_dbc_path: Path to demo DBC file
    """
    try:
        config = _load_config(demo)
    except ConfigValidationError as e:
        logger.error("Invalid configuration: %s", "; ".join(m.strip(" •") for m in e.errors))
        sys.exit(1)
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
    # A Demo bus's EV simulator logs its state every second.
    logging.getLogger("zelos_can.demo.demo").setLevel(logging.WARNING)

    if demo:
        logger.info("Demo mode enabled via --demo flag")

    # Determine output file if --file was specified
    output_file = None
    if file is not None:
        # If --file was given without a value, use UTC timestamp
        if str(file) == ".":
            utc_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            file = Path(f"{utc_timestamp}.trz")
        output_file = file
        logger.info(f"Recording trace to: {output_file}")

    # `choices=` providers run at form-render time, so registering needs no codecs yet.
    can_actions.register_actions(zelos_sdk.actions_registry)

    # Before the codecs, so their DBC merge summary and conflict warnings land in the trace.
    log_source_name, _, _ = trace_layout(prefix, LOG_SOURCE_NAME)
    global_source = zelos_sdk.init_global_source(log_source_name)
    shared_source = global_source if prefix else None
    if prefix:
        logger.info("Trace prefix: %s", prefix)
    else:
        logger.info("Trace prefix cleared: one trace source per bus")

    zelos_sdk.init(name=ACTION_PREFIX, log_level="info", actions=True)

    handler = TraceLoggingHandler(global_source)
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)

    # A recording opens before the codecs, so it captures startup: the DBC merge
    # summary and any conflict warnings land in `<prefix>/log` too.
    with contextlib.ExitStack() as stack:
        if output_file:
            stack.enter_context(zelos_sdk.TraceWriter(str(output_file)))

        # Each of these is a configuration mistake: report the reason, not a traceback.
        try:
            codec_pairs = _create_codecs(config, demo_dbc_path, advanced, shared_source)
        except (ValueError, FileNotFoundError, RuntimeError) as e:
            logger.error("CAN bus configuration is invalid: %s", e)
            sys.exit(1)
        codecs = [codec for codec, _ in codec_pairs]

        for codec, codec_name in codec_pairs:
            can_actions.CAN_CODECS[codec_name] = codec

        setup_shutdown_handler()

        bus_count = len(codecs)
        logger.info(f"Starting CAN extension with {bus_count} bus{'es' if bus_count > 1 else ''}")

        # A bus that cannot start exits with a one-line reason; every started bus is stopped.
        try:
            asyncio.run(_run_codecs_async(codecs, advanced["retry_unreachable_at_start"]))
        except (can.exceptions.CanError, ValueError, FileNotFoundError, RuntimeError) as e:
            logger.error("CAN bus failed: %s", e)
            sys.exit(1)
