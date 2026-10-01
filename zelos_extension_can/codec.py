"""CAN bus codec with database decoding and transmission."""

import asyncio
import json
import logging
import math
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import can
import cantools
import zelos_sdk

from .dbc import (
    DbcCatalog,
    describe_dbc_message,
    describe_dbc_message_summary,
    encode_dbc,
)
from .decode import FrameDecoder, TimestampMode
from .demo.demo import run_demo_ev_simulation
from .health import HEALTH_DETAIL, BusHealth
from .naming import DEFAULT_PREFIX, trace_layout
from .params import (
    parse_can_id,
    parse_data_hex,
    parse_mux,
    parse_signals_json,
    periodic_task_id,
    raw_slot,
    validate_id_range,
)
from .pcan import (
    PCAN_DEFAULT_BITRATE,
    PCAN_TIMING_KEYS,
    pcan_controller_state,
    pcan_fd_timing,
    release_pcan_channel,
)
from .periodics import Periodics
from .socketcan import SocketcanSupervisor, socketcan_health, socketcan_link

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Metrics:
    """Performance metrics for CAN codec operations."""

    messages_received: int = 0
    messages_decoded: int = 0
    decode_errors: int = 0
    # Unexpected failures in the decode -> emit path (schema registration,
    # signal conversion, ...) plus every frame skipped afterwards for a message
    # already known to fail. Deliberately separate from decode_errors, whose
    # semantics are bus noise (malformed frames) that existing consumers rely on.
    emit_errors: int = 0
    unknown_messages: int = 0
    # Every send that raised: one-shot send_raw / send_message, and each failed
    # period of a periodic.
    tx_errors: int = 0
    # Reserved for future BCM queue-overflow tracking; currently always 0.
    # The shape is kept stable so the app's wire contract doesn't churn.
    tx_overflows: int = 0
    # Error frames the adapter delivered. They report bus faults, not traffic,
    # so they are counted here and never traced or decoded as data.
    error_frames: int = 0
    # Reads that raised and were survived, such as a PCAN receive-queue overrun:
    # frames were lost, but the bus still works (see `_Reader`).
    rx_errors: int = 0


class _Reader:
    """The codec's listener on its Notifier. A failed read (a PCAN receive-queue
    overrun during an error flood, say) loses frames but leaves the bus usable,
    so it is reported and reading goes on, where python-can would end the reader
    and force a full reconnect. Failures that keep coming with no frame between,
    as from an unplugged adapter, still end it.

    It has no `stop()`: Notifier.stop() calls it on listeners, and the codec's
    own would stop the codec and its periodics on every reconnect."""

    # Back-to-back failed reads that mean the adapter itself is gone.
    GIVE_UP_AFTER = 50
    # Failures further apart than a read's timeout are separate incidents.
    BACK_TO_BACK_S = 1.0

    def __init__(self, on_message: Any, on_error: Any) -> None:
        self._on_message = on_message
        self._on_error = on_error
        self._failures = 0
        self._last_failure = -math.inf

    def __call__(self, msg: can.Message) -> None:
        self._failures = 0
        self._on_message(msg)

    def on_error(self, exc: Exception) -> None:
        if not isinstance(exc, can.CanOperationError):
            raise exc
        now = time.monotonic()
        back_to_back = now - self._last_failure < self.BACK_TO_BACK_S
        self._failures = self._failures + 1 if back_to_back else 1
        self._last_failure = now
        if self._failures >= self.GIVE_UP_AFTER:
            raise exc
        self._on_error(exc)


class CanCodec(can.Listener):
    """CAN bus monitor with database decoding and periodic transmission support."""

    def __init__(
        self,
        config: dict[str, Any],
        namespace: zelos_sdk.TraceNamespace | None = None,
        bus_name: str = "can_codec",
        source: Any = None,
    ) -> None:
        """Initialize CAN codec.

        :param config: Configuration dictionary with interface, channel, database_files
        :param namespace: Optional isolated TraceNamespace for the TraceSource
        :param bus_name: Name of this bus; the trace source name when `source`
            is None, the leading event-name segment when it is not
        :param source: Shared TraceSource to emit into. When given, every event
            this codec registers is nested under `{bus_name}/`; when None the
            codec owns a source named after the bus and events are unprefixed.
        """
        # `zelos-socketcan` is the pre-rename name of the native `socketcan` bus.
        if config.get("interface") == "zelos-socketcan":
            config["interface"] = "socketcan"
        self.config = config
        self.namespace = namespace
        self.bus_name = bus_name
        self.running = False
        self.last_message_time = time.time()
        self.start_time = time.time()

        # socketcan and ssh-socketcan both run the full recv -> DBC decode
        # -> trace pipeline in Rust (zelos_can.CanCodec): no python-can Notifier,
        # no cantools, and no per-frame Python on RX. self._native holds that
        # codec while running. TX actions still go through self.bus (a python-can
        # compat bus for socketcan, or a CodecTxAdapter over the Rust codec
        # for ssh-socketcan) so the bus-based action layer below is reused as-is.
        #
        #   - socketcan: Rust owns a real SocketCAN socket (Linux-only) and
        #     self-heals its recv loop internally.
        #   - ssh-socketcan: Rust decodes frames shuttled over ssh by a disposable
        #     SshTransport feeding a durable zelos_can.ExternalBus (any OS); the
        #     Python health supervisor rebuilds the transport on failure.
        #
        # self._use_rust unifies the "Rust owns RX/decode/schema/metrics" seams so
        # the native path stays byte-identical while ssh shares them.
        self._use_native = config.get("interface") == "socketcan"
        self._use_ssh = config.get("interface") == "ssh-socketcan"
        self._use_rust = self._use_native or self._use_ssh
        self._native: Any = None
        # Final counts of Rust codecs already stopped, by a reopen or by stop(),
        # so the totals get_tx_state reports never drop. Swapped with `_native`
        # under `_counts_lock`.
        self._native_carry: dict[str, int] = {}
        self._counts_lock = threading.Lock()
        # Serializes opening, reopening and stopping the bus with changes to its
        # periodics, so stop() never races a reopen that would revive the bus.
        self._lifecycle = threading.RLock()
        # ssh-socketcan only: the durable ExternalBus and the disposable
        # SshTransport (rebuilt on reconnect). None on every other interface.
        self._ebus: Any = None
        self._transport: Any = None
        # ssh-socketcan only: a session on THIS codec streamed a frame, so a
        # later "no such device" is the edge re-enumerating rather than a wrong
        # interface name. Per codec, not per transport — a transport rebuilt
        # after a working session inherits it (see _classify_ssh_failure). Set
        # only from a transport's proven `streamed`, never from construction:
        # the startup probe also passes an idle session.
        self._ssh_ever_connected = False
        # ssh-socketcan only: cansend failures counted on transports already
        # retired. The live transport's own count is added on read, so the
        # reported total never goes backwards across a rebuild.
        self._ssh_tx_errors_retired = 0

        self.timestamp_mode = TimestampMode[config.get("timestamp_mode", "auto").upper()]

        # Cache frequently accessed config values as booleans to avoid repeated string hashing
        self.log_raw_frames = config.get("log_raw_frames", False)
        self.fd_mode = config.get("fd_mode", False)
        self.emit_schemas_on_init = config.get("emit_schemas_on_init", False)

        # Metrics tracking
        self.metrics = Metrics()
        # Failed sends are counted from every periodic's thread at once.
        self._tx_failure_lock = threading.Lock()
        self._rx_failure_logged_at = -math.inf
        # Failed reopens since the bus was lost, and when one was last logged:
        # an adapter left unplugged is retried every cycle but logged once a minute.
        self._reopen_failures = 0
        self._reopen_failure_logged_at = -math.inf
        self.health = BusHealth(config.get("interface"))
        self._supervisor = (
            SocketcanSupervisor(str(config.get("channel")), bus_name)
            if config.get("interface") in ("socketcan", "socketcan-py")
            else None
        )

        # Demo mode simulation
        self.demo_mode = config.get("demo_mode", False)
        self.demo_task: asyncio.Task | None = None

        # python-can reader for non-Rust interfaces; stop() joins it before
        # shutting its bus down.
        self._notifier: can.Notifier | None = None

        self.catalog = DbcCatalog(config.get("database_files") or [])

        # Trace layout. A shared source means a prefix is configured and the
        # caller already named that source after it, so `source_name` is only
        # used on the prefix-less path (where it is the bus name).
        source_name, self.event_prefix, self.raw_event_name = trace_layout(
            DEFAULT_PREFIX if source is not None else "", self.bus_name
        )
        if source is not None:
            self.source = source
        else:
            self.source = (
                zelos_sdk.TraceSource(source_name, namespace=self.namespace)
                if self.namespace
                else zelos_sdk.TraceSource(source_name)
            )

        # On the Rust paths (socketcan / ssh-socketcan) the Rust codec owns
        # decode, the schemas (gated by its own emit_schemas_on_init) and the
        # raw-frame event (driven by `raw_event_name`).
        python_rx = not self._use_rust
        self.decoder = FrameDecoder(
            self.catalog,
            self.source,
            self.metrics,
            event_prefix=self.event_prefix,
            raw_event_name=self.raw_event_name if self.log_raw_frames and python_rx else None,
            timestamp_mode=self.timestamp_mode,
            emit_schemas_on_init=self.emit_schemas_on_init and python_rx,
        )

        # Log raw frame configuration
        if self.log_raw_frames:
            logger.info("Raw CAN frame logging is ENABLED - logging to '%s'", self.raw_event_name)
        else:
            logger.info("Raw CAN frame logging is DISABLED")

        self.bus: Any = None
        self.periodics = Periodics(bus_name, self._count_tx_failure)

    # Extension timestamp modes -> zelos_can.CanCodec modes. "absolute" maps to
    # "hardware" (kernel SO_TIMESTAMPNS, wall-clock on SocketCAN).
    _NATIVE_TIMESTAMP_MODE = {"AUTO": "auto", "ABSOLUTE": "hardware", "IGNORE": "ignore"}

    def _native_dbc_kwargs(self) -> dict[str, Any]:
        """DBC + trace-source kwargs shared by the two Rust codec paths.

        `event_prefix` nests decoded events under this bus on a shared source,
        matching the python-can path; cleared, it is None and events sit
        directly under the bus's own source.
        """
        kwargs: dict[str, Any] = {
            "database_file": [str(p) for p in self.catalog.database_files] or None,
            "source": self.source,
            "event_prefix": self.event_prefix,
        }
        if self.log_raw_frames:
            kwargs["raw_source"] = self.source
            kwargs["raw_event_name"] = self.raw_event_name
        return kwargs

    def start(self, *, quiet: bool = False) -> None:
        """Initialize CAN bus connection with retry logic.

        :param quiet: log at debug, for a reopen whose failures the caller summarizes
        """
        info, warn, error = (
            (logger.debug,) * 3 if quiet else (logger.info, logger.warning, logger.error)
        )
        info(
            "[%s] Starting CAN bus: interface=%s, channel=%s",
            self.bus_name,
            self.config["interface"],
            self.config["channel"],
        )

        if self._use_native and sys.platform != "linux":
            raise can.CanInterfaceNotImplementedError(
                "The 'socketcan' interface is Linux-only (it wraps the Rust "
                "zelos-can SocketCAN bus). Use 'pcan'/'kvaser'/'vector'/'slcan' on "
                "macOS/Windows, or 'ssh-socketcan' for a remote Linux device."
            )

        if self._use_native:
            self._start_native()
            return

        if self._use_ssh:
            self._start_ssh()
            return

        # `socketcan-py` is python-can's own `socketcan`.
        interface = self.config["interface"]
        bus_config = {
            "interface": "socketcan" if interface == "socketcan-py" else interface,
            "channel": self.config["channel"],
        }

        # Pass through optional bus config parameters if specified
        if "receive_own_messages" in self.config:
            bus_config["receive_own_messages"] = self.config["receive_own_messages"]

        if "bitrate" in self.config:
            bus_config["bitrate"] = self.config["bitrate"]

        if self.fd_mode:
            bus_config["fd"] = True
            if "data_bitrate" in self.config:
                bus_config["data_bitrate"] = self.config["data_bitrate"]

        # Merge additional config_json (advanced interface-specific options)
        if "config_json" in self.config and self.config["config_json"]:
            try:
                additional_config = json.loads(self.config["config_json"])
                logger.info("Merging additional config: %s", list(additional_config.keys()))
                bus_config.update(additional_config)
            except json.JSONDecodeError as e:
                logger.error("Failed to parse config_json: %s", e)
                raise ValueError(f"Invalid config_json: {e}") from e

        # PCAN opens CAN FD only from explicit bit timing, never from bitrates;
        # timing set by hand under Advanced wins.
        if self.fd_mode and interface == "pcan" and not PCAN_TIMING_KEYS & bus_config.keys():
            nominal = bus_config.pop("bitrate", PCAN_DEFAULT_BITRATE)
            bus_config["timing"] = pcan_fd_timing(nominal, bus_config.pop("data_bitrate", nominal))

        # PCAN's macOS library (PCBUSB) cannot echo TX frames, and python-can
        # fails the whole init on it.
        if bus_config["interface"] == "pcan" and sys.platform == "darwin":
            if bus_config.pop("receive_own_messages", False):
                warn(
                    "[%s] PCAN on macOS cannot receive its own messages; ignoring "
                    "receive_own_messages, so transmitted frames are not traced",
                    self.bus_name,
                )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.bus = can.Bus(**bus_config)
                self.running = True
                if self._supervisor is not None:
                    self._supervisor.note_link()
                info("[%s] CAN bus started successfully", self.bus_name)
                return
            except can.CanError as e:
                if bus_config["interface"] == "pcan":
                    release_pcan_channel(str(bus_config["channel"]))
                if attempt == max_retries - 1:
                    error(
                        "[%s] Failed to initialize CAN bus after %d attempts",
                        self.bus_name,
                        max_retries,
                    )
                    raise
                warn(
                    "[%s] Bus init failed (attempt %d/%d): %s",
                    self.bus_name,
                    attempt + 1,
                    max_retries,
                    e,
                )
                time.sleep(1)

    def _start_native(self) -> None:
        """Start the Rust-first pipeline for socketcan.

        zelos_can.CanCodec owns recv -> DBC decode -> trace entirely in Rust
        (no python-can Notifier, no cantools, no per-frame Python) and begins
        receiving on construction. A python-can compat bus is opened on the
        same channel purely for the TX action layer; it is never polled for RX,
        and TX frames are still traced because the Rust RX socket receives them
        via local loopback.
        """
        import zelos_can

        kwargs: dict[str, Any] = {
            **self._native_dbc_kwargs(),
            "channel": self.config["channel"],
            "log_raw_frames": self.log_raw_frames,
            "emit_schemas_on_init": self.emit_schemas_on_init,
            "timestamp_mode": self._NATIVE_TIMESTAMP_MODE.get(self.timestamp_mode.name, "auto"),
            "fd": self.fd_mode,
        }
        if self.config.get("rcvbuf_size") is not None:
            kwargs["rcvbuf_size"] = self.config["rcvbuf_size"]
        native = zelos_can.CanCodec(**kwargs)

        # TX-only python-can compat bus on the same channel; no Notifier is
        # attached so it does no RX work. The bus-based TX action layer
        # (send_raw / send_message / periodics) reuses this unchanged.
        try:
            self.bus = can.Bus(
                interface="zelos-socketcan", channel=self.config["channel"], fd=self.fd_mode
            )
        except BaseException:
            native.stop()
            raise
        with self._counts_lock:
            self._native = native
        self.running = True
        self._supervisor.note_link()
        logger.info("native socketcan codec started on %s", self.config["channel"])

    def _start_ssh(self) -> None:
        """Start the Rust-first pipeline for ssh-socketcan.

        Bridges a remote edge device's SocketCAN bus over ``ssh`` using the
        edge's own ``can-utils`` — nothing is deployed on the edge, so this runs
        on Linux/macOS/Windows. ``zelos_can.CanCodec`` owns decode -> trace -> TX
        channel -> periodics -> metrics entirely in Rust, fed by a durable
        ``zelos_can.ExternalBus``; an :class:`SshTransport` shuttles raw frames
        across ONE ssh session (``candump`` RX, a ``cansend`` loop on TX). The
        transport is disposable and is rebuilt on reconnect while the codec,
        ExternalBus, and armed periodics survive (see ``_reconnect_bus``).

        Mirrors ``_start_native`` but drives the codec through the ExternalBus
        seam instead of a SocketCAN socket: the ``channel``/``rcvbuf_size`` kwargs
        are SocketCAN-only and are replaced by ``bus=self._ebus`` (frame format
        flows per-frame through ``inject``). ``fd`` is passed through for parity
        with ``_start_native`` so the codec applies CAN-FD decode semantics.
        ``self._native`` is reused so metrics / get_tx_state / stop need no extra
        code. TX actions go through a ``CodecTxAdapter`` presenting the small
        python-can-shaped surface the action layer touches.
        """
        import zelos_can

        from .ssh_socketcan import CodecTxAdapter, SshTransport

        self._ebus = zelos_can.ExternalBus()
        self._native = zelos_can.CanCodec(
            **self._native_dbc_kwargs(),
            log_raw_frames=self.log_raw_frames,
            emit_schemas_on_init=self.emit_schemas_on_init,
            timestamp_mode=self._NATIVE_TIMESTAMP_MODE.get(self.timestamp_mode.name, "auto"),
            fd=self.fd_mode,
            bus=self._ebus,
        )

        try:
            self._transport = SshTransport(
                self._ebus,
                self.config["channel"],
                ssh_port=self.config.get("ssh_port", 22),
                ssh_key_path=self.config.get("ssh_key_path"),
                ssh_extra_opts=self.config.get("ssh_extra_opts"),
                ssh_host_key_policy=self.config.get("ssh_host_key_policy", "auto"),
                ssh_hw_timestamps=self.config.get("ssh_hw_timestamps", True),
                fd_mode=self.fd_mode,
                ever_connected=self._ssh_ever_connected,
            )
        except BaseException:
            # A transport that never came up leaves a live Rust codec behind,
            # and a bus whose start() raised is never handed to stop() (see
            # _run_codecs_async). Tear it down as stop() would, then re-raise.
            self._native.stop()
            self._native = None
            self._ebus = None
            raise
        # Contact is proven by a streamed frame, never by construction: the
        # probe passes an idle-but-alive session too, and a wrong remote_channel
        # on a slow connect must not turn "no such device" transient forever. A
        # session that starts streaming after the probe is picked up by
        # _rebuild_ssh_transport, which reads the transport it replaces.
        if self._transport.streamed:
            self._ssh_ever_connected = True
        self.bus = CodecTxAdapter(self._native, self._transport, self.config["channel"])
        self.running = True
        logger.info("ssh-socketcan codec started on %s", self.config["channel"])

    def stop(self) -> None:
        """Stop CAN bus and periodic tasks."""
        logger.info(f"[{self.bus_name}] Stopping CAN codec")
        # Set before waiting on a reopen in progress, so none starts, and again
        # after, since a reopen marks the bus running.
        self.running = False
        with self._lifecycle:
            self.running = False
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self.demo_task:
            self.demo_task.cancel()
            self.demo_task = None

        self.periodics.stop_all()
        self._retire_native()

        # Join the reader thread first, or it reads a shut-down bus.
        self._stop_notifier()

        if self.bus:
            self.bus.shutdown()
            self.bus = None

        # Best-effort drain so any frames still buffered in the TraceSource
        # batcher land in the trace before we go quiet. flush() may not exist
        # on older zelos-sdk; swallow that so a missing helper never blocks
        # shutdown.
        flush = getattr(self.source, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception as e:
                logger.debug("[%s] TraceSource.flush() raised during stop: %s", self.bus_name, e)

    def run(self) -> None:
        """Run async message reception loop."""
        asyncio.run(self._run_async())

    def _check_bus_health(self) -> bool:
        """Check if CAN bus is healthy.

        :return: True if bus is operational
        """
        if not self.bus:
            logger.debug("[%s] Bus health check: bus is None", self.bus_name)
            return False

        # For virtual/demo interfaces, just check if bus object exists
        if self.config.get("interface") == "virtual" or self.demo_mode:
            return True

        # For hardware interfaces, check bus state
        bus_state = self.bus.state
        is_active = bus_state == can.BusState.ACTIVE

        if not is_active:
            logger.info(
                "[%s] Bus health check failed: state is %s, expected ACTIVE",
                self.bus_name,
                bus_state.name,
            )
            return False

        # A PCAN controller that went bus-off stays off until it is initialized
        # again, so a moment's wiring fault would end all traffic for good.
        if pcan_controller_state(self.bus) == "bus_off":
            logger.warning("[%s] The controller is bus-off; restarting the bus", self.bus_name)
            return False

        return True

    async def _reconnect_bus(self) -> bool:
        """Attempt to reconnect to CAN bus.

        :return: True if reconnection successful
        """
        logger.debug("[%s] Attempting bus reconnection...", self.bus_name)

        # ssh-socketcan: the Rust codec, the ExternalBus, and armed periodics are
        # DURABLE — only the SshTransport is disposable. Reconnect rebuilds the
        # transport ONLY and must NEVER run the generic rebuild below: that path
        # would build a second ExternalBus + CanCodec against the SAME trace
        # source (double-emitting every frame), orphan the live transport, and
        # reset RX counters. The blocking teardown + Popen spawns run off the
        # event loop so a wedged transport can't stall other buses' supervisors.
        if self._use_ssh:
            return await asyncio.to_thread(self._rebuild_ssh_transport)

        # Off the event loop: closing a vanished adapter and opening it again
        # block, and every bus is supervised on this loop.
        try:
            await asyncio.to_thread(self._close_bus)
            await asyncio.sleep(1)
            await asyncio.to_thread(self._reopen_bus)
            return True
        except Exception as e:
            self._note_reopen_failure(e)
            return False

    def _close_bus(self) -> None:
        with self._lifecycle:
            if self.bus:
                bus, self.bus = self.bus, None
                # A vanished device fails its own shutdown; that must not block reopening.
                try:
                    bus.shutdown()
                except Exception as e:
                    logger.warning("[%s] Old bus shutdown failed: %s", self.bus_name, e)
            self.periodics.forget_tasks()

    def _reopen_bus(self) -> None:
        with self._lifecycle:
            if not self.running:
                return
            self.start(quiet=self._reopen_failures > 0)
            # shutdown() stopped the periodics with the old bus; a failed
            # start leaves them pending for the next attempt.
            self.periodics.rearm(self.bus)

    def _note_reopen_failure(self, error: Exception) -> None:
        """Log a failed reopen: the first in full, then once a minute with a count."""
        self._reopen_failures += 1
        now = time.monotonic()
        if now - self._reopen_failure_logged_at < 60:
            return
        self._reopen_failure_logged_at = now
        if self._reopen_failures == 1:
            logger.error("[%s] Reconnecting failed, retrying: %s", self.bus_name, error)
        else:
            logger.error(
                "[%s] Still reconnecting after %d attempts: %s",
                self.bus_name,
                self._reopen_failures,
                error,
            )

    def _rebuild_ssh_transport(self) -> bool:
        """Rebuild ONLY the ssh transport; codec + ExternalBus + periodics survive.

        Runs off the event loop (via ``asyncio.to_thread``): the teardown joins
        three threads and waits on the ssh proc (~4 s worst case) then spawns a
        fresh session. Returns True on a clean rebuild, False on a transient
        failure — the codec, its ``self.bus`` object identity, the ExternalBus,
        and armed periodics are then left untouched and the supervisor retries
        next tick. A permanent failure propagates instead (see below). Never
        touches ``self._native`` / ``self._ebus`` / ``self.bus`` identity.
        """
        if not self.running:
            return False
        if self._native is None or self._ebus is None:
            logger.error(
                "[%s] ssh reconnect: codec not initialized (native=%s ebus=%s); "
                "cannot rebuild transport",
                self.bus_name,
                self._native is not None,
                self._ebus is not None,
            )
            return False

        from .ssh_socketcan import SshPermanentError, SshTransport

        # Reap the dead procs/threads first (idempotent + total), then drop the
        # reference: a failed rebuild below must not leave the supervisor reading
        # a torn-down transport's stderr ring.
        if self._transport is not None:
            # Read the outgoing transport before it goes: it is the only party
            # that knows whether this bus ever streamed a frame, and its TX
            # failures must survive in the reported total.
            if self._transport.streamed:
                self._ssh_ever_connected = True
            self._ssh_tx_errors_retired += self._transport.tx_errors
            try:
                self._transport.teardown()
            except Exception:
                logger.exception(
                    "[%s] ssh reconnect: transport teardown raised (continuing)", self.bus_name
                )
            self._transport = None

        # stop() may have raced us during the blocking teardown — bail before we
        # resurrect a transport on a codec that is shutting down.
        if not self.running:
            return False

        try:
            # Discard stale periodic backlog queued while the link was down.
            self._ebus.drain_tx()
            self._transport = SshTransport(
                self._ebus,
                self.config["channel"],
                ssh_port=self.config.get("ssh_port", 22),
                ssh_key_path=self.config.get("ssh_key_path"),
                ssh_extra_opts=self.config.get("ssh_extra_opts"),
                ssh_host_key_policy=self.config.get("ssh_host_key_policy", "auto"),
                ssh_hw_timestamps=self.config.get("ssh_hw_timestamps", True),
                fd_mode=self.fd_mode,
                ever_connected=self._ssh_ever_connected,
            )
            self.bus.transport = self._transport
            logger.info("[%s] ssh transport rebuilt, codec preserved", self.bus_name)
            return True
        except SshPermanentError:
            raise  # the class is the verdict: propagate, never retry
        except Exception as e:
            logger.warning(
                "[%s] ssh transport rebuild failed, retrying next tick: %s", self.bus_name, e
            )
            return False

    def on_message_received(self, message: can.Message) -> None:
        """Handle CAN message directly from notifier (can.Listener interface).

        This direct callback approach is more efficient than AsyncBufferedReader
        as it eliminates buffering overhead and async context switching.

        :param message: Received CAN message
        """
        if message.is_error_frame:
            self.metrics.error_frames += 1
            self.health.note_error_frame(message)
        else:
            self.decoder.handle(message)
        self.last_message_time = time.time()

    def _check_notifier_health(self, notifier: can.Notifier | None) -> bool:
        """Check if notifier threads are alive.

        :param notifier: CAN notifier instance
        :return: True if at least one notifier thread is alive
        """
        try:
            if not hasattr(notifier, "_readers"):
                return False

            for reader in notifier._readers:
                if isinstance(reader, threading.Thread):
                    if reader.is_alive():
                        return True
                    logger.debug(
                        "[%s] Notifier thread '%s' is not alive", self.bus_name, reader.name
                    )

            logger.debug("[%s] No alive notifier threads found", self.bus_name)
            return False
        except Exception as e:
            logger.error(
                "[%s] Exception while checking notifier thread status: %s", self.bus_name, e
            )
            return False

    def _log_reconnection_reason(self, notifier_alive: bool, bus_healthy: bool) -> None:
        """Log detailed reason for reconnection.

        :param notifier_alive: Whether notifier threads are alive
        :param bus_healthy: Whether bus health check passed
        """
        if not notifier_alive and not bus_healthy:
            logger.error(
                "[%s] Reconnection triggered: Both notifier thread stopped AND bus unhealthy",
                self.bus_name,
            )
        elif not notifier_alive:
            logger.error(
                "[%s] Reconnection triggered: Notifier thread stopped (bus was healthy)",
                self.bus_name,
            )
        else:
            logger.error(
                "[%s] Reconnection triggered: Bus health check failed (notifier was alive)",
                self.bus_name,
            )

    def _start_notifier(self) -> None:
        self._notifier = can.Notifier(
            self.bus, [_Reader(self.on_message_received, self._on_receive_error)]
        )

    def _stop_notifier(self) -> None:
        """Stop and join the python-can reader, if any."""
        if self._notifier is not None:
            self._notifier.stop()
            self._notifier = None

    async def _handle_reconnection(self) -> None:
        """Handle bus reconnection and notifier recreation."""
        # Joining the reader blocks too; see _reconnect_bus.
        await asyncio.to_thread(self._stop_notifier)
        if not await self._reconnect_bus() or self.bus is None:
            return
        self._start_notifier()
        if self._reopen_failures:
            logger.info(
                "[%s] Reconnected after %d failed attempts", self.bus_name, self._reopen_failures
            )
        self._reopen_failures, self._reopen_failure_logged_at = 0, -math.inf

    async def _run_async(self) -> None:
        """Main async loop - health monitoring and reconnection handling.

        Message reception happens via on_message_received() callback, not in this loop.
        This approach is more efficient than AsyncBufferedReader + asyncio.wait_for().
        """
        # Native path: zelos_can.CanCodec runs its own Rust recv/decode/trace
        # loop, with no python-can Notifier and no per-frame Python. Its socket
        # stays bound to an interface that was unplugged, so the supervisor
        # reopens it on the replugged one.
        if self._use_native:
            logger.info("[%s] Starting CAN rx (native socketcan pipeline)", self.bus_name)
            try:
                while self.running:
                    await asyncio.sleep(5.0)
                    try:
                        if await asyncio.to_thread(self._supervisor.look):
                            await asyncio.to_thread(self._restart_native)
                    except Exception as e:
                        # One bus failing to recover must not end every bus.
                        logger.warning(
                            "[%s] Recovering the interface failed, retrying: %s", self.bus_name, e
                        )
            except asyncio.CancelledError:
                logger.info("[%s] CAN reader cancelled", self.bus_name)
            return

        # ssh-socketcan path: the Rust codec owns RX/decode/trace/metrics (fed by
        # the ExternalBus), so there is no python-can Notifier. Unlike the native
        # SocketCAN codec (which self-heals internally), the ssh procs live in
        # Python, so a lightweight 5 s supervisor watches transport health via the
        # adapter's bus.state and rebuilds only the transport on failure — codec,
        # ExternalBus, and armed periodics survive the rebuild.
        if self._use_ssh:
            from .ssh_socketcan import SshPermanentError

            logger.info("[%s] Starting CAN rx (ssh-socketcan pipeline)", self.bus_name)
            # Capped backoff so a long edge outage doesn't spam thousands of
            # rebuild/log cycles: probe every 5 s when healthy; on a failed
            # rebuild grow the interval (5 s → cap 60 s), reset to 5 s on success.
            healthy_interval = 5.0
            max_interval = 60.0
            interval = healthy_interval
            try:
                while self.running:
                    await asyncio.sleep(interval)
                    if self._check_bus_health():
                        interval = healthy_interval
                        continue
                    # Judge the link BEFORE reconnecting, so the verdict comes from
                    # the genuine failure and not teardown "Killed" noise.
                    failure = (
                        self._transport.classify_failure() if self._transport is not None else None
                    )
                    if isinstance(failure, SshPermanentError):
                        raise failure  # the class is the verdict
                    # Transient: name the cause (unreachable / timed out / candump
                    # died) instead of a bare "unhealthy".
                    reason = self._transport.stderr_tail() if self._transport is not None else ""
                    if reason:
                        logger.error(
                            "[%s] Reconnection triggered: ssh transport unhealthy (ssh: %s)",
                            self.bus_name,
                            reason,
                        )
                    else:
                        logger.error(
                            "[%s] Reconnection triggered: ssh transport unhealthy", self.bus_name
                        )
                    if await self._reconnect_bus():
                        interval = healthy_interval
                    else:
                        interval = min(interval * 2, max_interval)
            except asyncio.CancelledError:
                logger.info("[%s] CAN reader cancelled", self.bus_name)
            except can.exceptions.CanError:
                raise  # permanent ssh failure: the app layer reports and exits
            except Exception as e:
                logger.exception(
                    "[%s] Error in ssh-socketcan supervision loop: %s", self.bus_name, e
                )
            return

        if not self.bus:
            logger.error("[%s] Bus not initialized, call start() first", self.bus_name)
            return

        self._start_notifier()

        if self.demo_mode:
            self.demo_task = asyncio.create_task(
                run_demo_ev_simulation(self.bus, self.catalog.messages_by_name, self)
            )
            logger.info("[%s] Started EV simulation task for demo mode", self.bus_name)

        try:
            logger.info("[%s] Starting CAN message rx loop", self.bus_name)
            while self.running:
                await asyncio.sleep(5.0)
                if self._supervisor is not None:
                    try:
                        replaced = await asyncio.to_thread(self._supervisor.look)
                    except Exception as e:
                        replaced = False
                        logger.warning(
                            "[%s] Recovering the interface failed, retrying: %s", self.bus_name, e
                        )
                    if replaced:
                        await self._handle_reconnection()
                        continue

                notifier_alive = self._check_notifier_health(self._notifier)
                bus_healthy = self._check_bus_health()

                if not notifier_alive or not bus_healthy:
                    # The cause once per outage; failed reopens are summarized.
                    if not self._reopen_failures:
                        self._log_reconnection_reason(notifier_alive, bus_healthy)
                    await self._handle_reconnection()
        except asyncio.CancelledError:
            logger.info("[%s] CAN reader cancelled", self.bus_name)
        except Exception as e:
            logger.exception("[%s] Error in CAN reception loop: %s", self.bus_name, e)
        finally:
            # The reader is stopped by stop(), after the periodics, so none go untraced.
            logger.info("[%s] CAN reception stopped", self.bus_name)

    # ─── Operations exposed by the free-floating actions module ────────────
    #
    # These methods are the implementation backing the global `can/<name>`
    # action surface defined in `zelos_extension_can.actions`. They're kept as
    # plain methods (no @action decorators) so `actions.py` can hold the
    # decorator stack and `choices=_available_codecs` lives at module scope.

    _NATIVE_RX_COUNTS = ("messages_received", "messages_decoded", "unknown_messages")
    # A stalled ssh transport surfaces here (the Rust codec's TX channel/outlet),
    # not in the Python-side self.metrics.
    _NATIVE_TX_COUNTS = ("tx_errors", "tx_overflows")

    def _native_counts(self, names: tuple[str, ...]) -> dict[str, int]:
        """Counters for the Rust paths: the live codec's plus those of codecs
        already stopped. Counters are 0 before start."""
        with self._counts_lock:
            native, carry = self._native, self._native_carry
        live = native.metrics() if native is not None else None
        return {n: carry.get(n, 0) + (getattr(live, n) if live else 0) for n in names}

    def _native_rx_counts(self) -> dict[str, int]:
        return self._native_counts(self._NATIVE_RX_COUNTS)

    def _native_tx_counts(self) -> dict[str, int]:
        return self._native_counts(self._NATIVE_TX_COUNTS)

    def _retire_native(self) -> None:
        """Stop the Rust codec and fold its final counts into the carry.

        It is stopped first, and keeps its counts, so a snapshot taken while
        it is retired never reads lower than one taken before."""
        native = self._native
        if native is None:
            return
        native.stop()
        final = native.metrics()
        names = self._NATIVE_RX_COUNTS + self._NATIVE_TX_COUNTS
        with self._counts_lock:
            carry = self._native_carry
            self._native_carry = {n: carry.get(n, 0) + getattr(final, n) for n in names}
            self._native = None

    def _ssh_tx_error_count(self) -> int:
        """Remote writes cansend reported failing, across transport rebuilds.

        The Rust codec hands every frame to the transport successfully, so a
        failed `cansend` shows up NOWHERE else: the transport counts the lines
        it wrote to the session's stderr, and a retired transport's total is
        folded in when it is replaced.
        """
        live = self._transport.tx_errors if self._transport is not None else 0
        return self._ssh_tx_errors_retired + live

    def get_tx_state(self) -> dict[str, Any]:
        # Extension id/version/state intentionally NOT included — that info
        # is canonical at the `extensions.list` bridge surface and the webapp
        # consumes it from there, not from this 1 Hz polled action.
        db_path = self.catalog.first_file
        # On the Rust paths (socketcan / ssh-socketcan) RX counters live in
        # the Rust codec. TX counters merge the Python-side self.metrics (one-shot
        # send failures via the bus/adapter) with the Rust codec's own tx counters
        # (a stalled ssh transport surfaces there, not in self.metrics). For the
        # native socketcan path TX goes through a separate python-can compat
        # bus so the Rust tx counters stay 0 — the reported values are unchanged.
        tx_errors = self.metrics.tx_errors
        tx_overflows = self.metrics.tx_overflows
        if self._use_rust:
            rx = self._native_rx_counts()
            native_tx = self._native_tx_counts()
            tx_errors += native_tx["tx_errors"]
            tx_overflows += native_tx["tx_overflows"]
            # ssh-socketcan adds one more source: the remote cansend, whose
            # failures only ever appear on the session's stderr.
            if self._use_ssh:
                tx_errors += self._ssh_tx_error_count()
        else:
            rx = {
                "messages_received": self.metrics.messages_received,
                "messages_decoded": self.metrics.messages_decoded,
                "unknown_messages": self.metrics.unknown_messages,
                "error_frames": self.metrics.error_frames,
                "rx_errors": self.metrics.rx_errors,
            }
        adapter = self._adapter_health() if self.running and self.bus is not None else None
        status, health = self.health.report(self.running, self.bus, *(adapter or (None, None, {})))
        return {
            "captured_at_unix_ms": int(time.time() * 1000),
            "bus": {
                "name": self.bus_name,
                "interface": self.config.get("interface", "unknown"),
                "channel": self.config.get("channel"),
                "status": status,
                "health": health,
                # `dbc` is the legacy single-DBC view the tx webapp reads
                # (first file, combined hash); `dbcs` is the full list.
                "dbc": {
                    "path": str(db_path) if db_path else None,
                    "name": db_path.name if db_path else None,
                    "hash": self.catalog.dbc_hash,
                    "message_count": len(self.catalog.messages),
                },
                "dbcs": self.catalog.dbc_entries,
                # `dbc_conflicts` is one id+name two files laid out differently
                # (later wins); `dbc_overlaps` is one id under several names,
                # all of which survive and decode.
                "dbc_conflicts": self.catalog.dbc_conflicts,
                "dbc_overlaps": self.catalog.dbc_overlaps,
                "metrics": {
                    "tx_errors": tx_errors,
                    "tx_overflows": tx_overflows,
                    **rx,
                },
                "periodics": self.periodics.snapshot(),
            },
        }

    def list_messages(self) -> dict[str, Any]:
        """Every surviving definition, one entry per `key`, in file order.

        The key is the definition's trace event name, and is what the TX calls
        address. A name at two ids therefore gets two rows, each transmittable;
        nothing is hidden behind a last-wins rule.
        """
        db_path = self.catalog.first_file
        return {
            "bus": self.bus_name,
            "dbc_name": db_path.name if db_path else None,
            "dbcs": [path.name for path in self.catalog.database_files],
            "messages": [
                {
                    "key": key,
                    **describe_dbc_message_summary(msg),
                    "database": self.catalog.database_of(msg),
                }
                for key, msg in self.catalog.messages_by_key.items()
            ],
        }

    def describe_message(self, message: str) -> dict[str, Any]:
        """Detail for one definition, addressed by key or unambiguous name."""
        dbc_msg = self.catalog.resolve(message)
        db_path = self.catalog.first_file
        return {
            "bus": self.bus_name,
            "dbc_name": db_path.name if db_path else None,
            "dbcs": [path.name for path in self.catalog.database_files],
            "message": {
                "key": self.catalog.key_of(dbc_msg),
                **describe_dbc_message(dbc_msg),
                "database": self.catalog.database_of(dbc_msg),
            },
        }

    def send_raw(
        self,
        can_id: str,
        data: str,
        is_extended: bool = False,
        is_fd: bool = False,
    ) -> dict[str, Any]:
        self._require_running()
        can_id_int = parse_can_id(can_id)
        validate_id_range(can_id_int, is_extended)
        data_bytes = parse_data_hex(data)
        msg = self._frame(can_id_int, data_bytes, is_extended, is_fd)
        self._send_or_count(msg)
        return {
            "can_id": can_id_int,
            "can_id_hex": f"0x{can_id_int:x}",
            "dlc": len(data_bytes),
            "data_hex": data_bytes.hex(),
            "is_extended": is_extended,
            "is_fd": is_fd,
        }

    def start_periodic_raw(
        self,
        can_id: str,
        data: str,
        period_ms: int = 100,
        is_extended: bool = False,
        is_fd: bool = False,
    ) -> dict[str, Any]:
        self._require_running()
        can_id_int = parse_can_id(can_id)
        validate_id_range(can_id_int, is_extended)
        data_bytes = parse_data_hex(data)
        msg = self._frame(can_id_int, data_bytes, is_extended, is_fd)
        tid = periodic_task_id(raw_slot(can_id_int, is_extended), "raw")
        replaced = self._stop_periodic_slot(tid)
        slot = {
            "task_id": tid,
            "can_id": can_id_int,
            "is_extended": is_extended,
            "is_fd": is_fd,
            "dlc": len(data_bytes),
            "data_hex": data_bytes.hex(),
            "period_ms": period_ms,
            "mode": "raw",
            "is_active": True,
        }
        self._start_periodic(tid, msg, period_ms / 1000.0, "raw", slot)
        return {"task_id": tid, "replaced": replaced}

    def send_message(self, message: str, signals_json: str, mux: str = "") -> dict[str, Any]:
        self._require_running()
        signals = parse_signals_json(signals_json)
        dbc_msg = self.catalog.resolve(message)
        mux_value = parse_mux(mux)
        data_bytes = encode_dbc(dbc_msg, signals, mux_value)
        msg = self._dbc_frame(dbc_msg, data_bytes)
        self._send_or_count(msg)
        return {
            "message": message,
            "can_id": dbc_msg.frame_id,
            "can_id_hex": f"0x{dbc_msg.frame_id:x}",
            "dlc": len(data_bytes),
            "data_hex": data_bytes.hex(),
            "mux": mux_value,
        }

    def encode_preview(self, message: str, signals_json: str, mux: str = "") -> dict[str, Any]:
        signals = parse_signals_json(signals_json)
        dbc_msg = self.catalog.resolve(message)
        mux_value = parse_mux(mux)
        data_bytes = encode_dbc(dbc_msg, signals, mux_value)
        return {
            "message": message,
            "can_id": dbc_msg.frame_id,
            "can_id_hex": f"0x{dbc_msg.frame_id:x}",
            "dlc": len(data_bytes),
            "data_hex": data_bytes.hex(),
            "mux": mux_value,
        }

    def start_periodic_message(
        self,
        message: str,
        signals_json: str,
        period_ms: int = 100,
        mux: str = "",
    ) -> dict[str, Any]:
        self._require_running()
        signals = parse_signals_json(signals_json)
        dbc_msg = self.catalog.resolve(message)
        mux_value = parse_mux(mux)
        data_bytes = encode_dbc(dbc_msg, signals, mux_value)
        msg = self._dbc_frame(dbc_msg, data_bytes)
        mux_key = "dbc" if mux_value is None else f"mux={mux_value}"
        # Keyed per definition: two same-name messages at different ids each
        # hold their own slot.
        key = self.catalog.key_of(dbc_msg)
        tid = periodic_task_id(key, mux_key)
        replaced = self._stop_periodic_slot(tid)
        slot = {
            "task_id": tid,
            "can_id": dbc_msg.frame_id,
            "is_extended": dbc_msg.is_extended_frame,
            "is_fd": msg.is_fd,
            "dlc": len(data_bytes),
            "data_hex": data_bytes.hex(),
            "period_ms": period_ms,
            "mode": "dbc",
            "is_active": True,
            "message": {"name": message, "mux": mux_value, "signals": signals},
        }
        self._start_periodic(tid, msg, period_ms / 1000.0, "dbc", slot)
        return {"task_id": tid, "replaced": replaced}

    def stop_periodic(self, task_id: str) -> dict[str, Any]:
        stopped = self._stop_periodic_slot(task_id)
        return {"task_id": task_id, "stopped": stopped}

    # ─── Internals shared by the action methods above ────────────────────

    def _frame(self, can_id: int, data: bytes, is_extended: bool, is_fd: bool) -> can.Message:
        """A frame this bus can carry. A driver fails one it cannot only when
        it is sent, which for a periodic is every period."""
        if is_fd and not self.fd_mode:
            raise ValueError(
                f"bus '{self.bus_name}' runs classic CAN; turn on CAN-FD Mode to send CAN FD frames"
            )
        return can.Message(
            arbitration_id=can_id, data=data, is_extended_id=is_extended, is_fd=is_fd, check=True
        )

    def _dbc_frame(self, dbc_msg: cantools.database.can.Message, data: bytes) -> can.Message:
        """``dbc_msg`` as a frame this bus can carry. A DBC marks a CAN FD message
        with ``VFrameFormat``; on a bus without CAN-FD Mode, one that fits a
        classic frame goes out as one. Without the mark, one over 8 bytes fits
        no frame."""
        if not dbc_msg.is_fd and len(data) > 8:
            raise ValueError(
                f"{dbc_msg.name} is {len(data)} bytes, but its DBC doesn't mark it CAN FD "
                "(VFrameFormat), and a classic CAN frame carries at most 8"
            )
        is_fd = dbc_msg.is_fd and (self.fd_mode or len(data) > 8)
        return self._frame(dbc_msg.frame_id, data, dbc_msg.is_extended_frame, is_fd)

    def _require_running(self) -> None:
        if not self.running:
            raise RuntimeError(f"bus '{self.bus_name}' is not running")
        if not self.bus:
            raise RuntimeError(f"bus '{self.bus_name}' is reconnecting; try again shortly")

    def _start_periodic(
        self, tid: str, msg: can.Message, period_s: float, mode: str, slot: dict[str, Any]
    ) -> None:
        with self._lifecycle:
            self._require_running()
            self.periodics.start(self.bus, tid, msg, period_s, mode, slot)

    def _stop_periodic_slot(self, tid: str) -> bool:
        with self._lifecycle:
            return self.periodics.stop(tid)

    def _send_or_count(self, msg: can.Message) -> None:
        """Wrapper around bus.send() that counts CanError as tx_errors and
        re-raises with a friendlier message. Used by the one-shot send_raw /
        send_message paths; a periodic's failures reach `_count_tx_failure`
        through its task."""
        try:
            self.bus.send(msg)
        except can.CanError as e:
            self._count_tx_failure(e)
            raise RuntimeError(f"send failed on bus '{self.bus_name}': {e}") from e

    def _count_tx_failure(self, exc: Exception) -> None:
        with self._tx_failure_lock:
            self.metrics.tx_errors += 1
        self.health.note_tx_failure(str(exc))

    def _on_receive_error(self, exc: Exception) -> None:
        """Count a failed read the reader survived; it marks the bus's health."""
        now = time.monotonic()
        self.metrics.rx_errors += 1
        self.health.note_rx_failure(str(exc))
        if now - self._rx_failure_logged_at >= 60.0:
            self._rx_failure_logged_at = now
            logger.warning("[%s] Frames were lost on receive: %s", self.bus_name, exc)

    def _adapter_health(self) -> tuple[str | None, str | None, dict[str, int]]:
        """The error state the adapter itself reports, why, and its error
        counters; None for a state it does not report."""
        if self.config.get("interface") in ("socketcan", "socketcan-py"):
            iface = str(self.config.get("channel"))
            link = socketcan_link(iface)
            if link is None:
                return None, None, {}
            settings = link.settings or self._supervisor.settings
            state, detail = socketcan_health(iface, link, settings)
            return state, detail, link.counters or {}
        state = pcan_controller_state(self.bus)
        return state, HEALTH_DETAIL.get(state) if state else None, {}

    def _restart_native(self) -> None:
        """Reopen the native codec and its TX bus on a replugged interface,
        keeping the counts and re-arming the periodics."""
        with self._lifecycle:
            if not self.running:
                return
            self.periodics.halt()
            self._retire_native()
            if self.bus is not None:
                bus, self.bus = self.bus, None
                try:
                    bus.shutdown()
                except Exception as e:
                    logger.warning("[%s] Old bus shutdown failed: %s", self.bus_name, e)
            self._start_native()
            self.periodics.rearm(self.bus)
        logger.info(
            "[%s] reopened %s after it was unplugged", self.bus_name, self.config["channel"]
        )
