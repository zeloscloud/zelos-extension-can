"""Cross-bus action router under the `can` namespace.

Mirrors `features/CAN_TRANSMIT.md` §5 Block D action contract:

    can/get_tx_state           — single source of truth: buses + periodics + metrics
    can/list_messages          — DBC catalog for a bus (read from the already-loaded DBC)
    can/send_raw               — one-shot raw frame
    can/start_periodic_raw     — raw periodic; returns {task_id, replaced}
    can/send_message           — one-shot DBC-encoded message
    can/start_periodic_message — DBC periodic; returns {task_id, replaced}
    can/stop_periodic          — stop by stable task_id

The per-codec `<bus>/...` actions on `CanCodec` are unchanged so the existing
desktop actions panel keeps working; this router is additive surface only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from importlib import metadata
from pathlib import Path
from typing import Any

import can
import cantools
from zelos_sdk.actions import action

from ..codec import CanCodec

logger = logging.getLogger(__name__)

EXTENSION_ID = "zeloscloud.zelos-extension-can"


def _extension_version() -> str:
    try:
        return metadata.version("zelos-extension-can")
    except metadata.PackageNotFoundError:
        return "unknown"


def _parse_can_id(can_id: str) -> int:
    """Accept `0x100`, `100`, or hex without prefix; reject negatives and overflow at the caller."""
    s = can_id.strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    return int(s, 16)


def _parse_data_hex(data: str) -> bytes:
    return bytes.fromhex(data.replace(" ", "").replace(",", ""))


def _validate_id_range(can_id: int, is_extended: bool) -> None:
    max_id = 0x1FFFFFFF if is_extended else 0x7FF
    if can_id < 0 or can_id > max_id:
        kind = "extended" if is_extended else "standard"
        raise ValueError(f"can_id 0x{can_id:x} out of range for {kind} ID (max 0x{max_id:x})")


def _task_id(bus: str, can_id: int, is_extended: bool, mux: str = "raw") -> str:
    """Stable taskId — bus + arbitration ID + frame kind + mux/raw discriminator.

    Two periodics with the same key replace each other; this is the contract.
    Apps that want a sibling slot must pick a different mux or use a DBC message.
    See CAN_TRANSMIT.md §2 decision 10 and Block E.
    """
    ext = "ext" if is_extended else "std"
    return f"{bus}:0x{can_id:x}:{ext}:{mux}"


class CanActionsRouter:
    """Routes the `can/...` action surface to per-bus :class:`CanCodec` instances.

    Holds a registry of stable taskId → asyncio.Task for periodics started
    through this router. Coexists with each codec's own `periodic_tasks` dict
    (used by the legacy `<bus>/start_periodic` action surface).
    """

    def __init__(self, codecs: dict[str, CanCodec]) -> None:
        self._codecs = codecs
        self._tasks: dict[str, asyncio.Task] = {}
        # Slot metadata so get_tx_state can reconstruct what each task is sending
        # without poking the asyncio.Task internals.
        self._slots: dict[str, dict[str, Any]] = {}

    # ─── Bus lookup helpers ─────────────────────────────────────────────

    def _codec(self, bus: str) -> CanCodec:
        codec = self._codecs.get(bus)
        if codec is None:
            available = sorted(self._codecs.keys())
            raise ValueError(f"unknown bus '{bus}'. Available: {available}")
        if not codec.running or not codec.bus:
            raise RuntimeError(f"bus '{bus}' is not running")
        return codec

    # ─── State snapshot ─────────────────────────────────────────────────

    @action("Get TX State", "Stateless snapshot of buses, periodics, and bus-health metrics")
    def get_tx_state(self) -> dict[str, Any]:
        return {
            "capturedAtUnixMs": int(time.time() * 1000),
            "extension": {
                "id": EXTENSION_ID,
                "version": _extension_version(),
                "state": "running" if self._codecs else "stopped",
            },
            "buses": [
                self._bus_snapshot(name, codec) for name, codec in sorted(self._codecs.items())
            ],
        }

    def _bus_snapshot(self, name: str, codec: CanCodec) -> dict[str, Any]:
        db_path = Path(codec.database_file_path)
        return {
            "name": name,
            "interface": codec.config.get("interface", "unknown"),
            "channel": codec.config.get("channel"),
            "status": "active" if codec.running and codec.bus else "stopped",
            "dbc": {
                "path": str(db_path),
                "name": db_path.name,
                "messageCount": len(codec.db.messages),
            },
            "metrics": {
                # tx_errors / tx_overflows are not tracked yet — surface zero so the
                # shape matches the app's TS contract; Phase 2b wires real counters.
                "txErrors": 0,
                "txOverflows": 0,
                "messagesReceived": codec.metrics.messages_received,
                "messagesDecoded": codec.metrics.messages_decoded,
                "unknownMessages": codec.metrics.unknown_messages,
            },
            "periodics": [
                self._slots[tid]
                for tid in sorted(self._slots)
                if self._slots[tid].get("bus") == name
            ],
        }

    # ─── DBC catalog ────────────────────────────────────────────────────

    @action("List Messages", "DBC message + signal catalog for a bus (from the loaded DBC)")
    @action.text("bus", title="Bus")
    def list_messages(self, bus: str) -> dict[str, Any]:
        codec = self._codec(bus)
        db_path = Path(codec.database_file_path)
        return {
            "bus": bus,
            "dbcName": db_path.name,
            "messages": [_describe_dbc_message(msg) for msg in codec.db.messages],
        }

    # ─── Raw send + periodic ────────────────────────────────────────────

    @action("Send Raw", "Send a one-shot raw CAN frame")
    @action.text("bus", title="Bus")
    @action.text("can_id", title="CAN ID (hex)", placeholder="0x100")
    @action.text("data", title="Data (hex bytes)", placeholder="01 02 03 04", default="")
    @action.boolean("is_extended", title="Extended ID (29-bit)", default=False, widget="toggle")
    @action.boolean("is_fd", title="CAN FD", default=False, widget="toggle")
    def send_raw(
        self,
        bus: str,
        can_id: str,
        data: str,
        is_extended: bool = False,
        is_fd: bool = False,
    ) -> dict[str, Any]:
        codec = self._codec(bus)
        can_id_int = _parse_can_id(can_id)
        _validate_id_range(can_id_int, is_extended)
        data_bytes = _parse_data_hex(data)
        msg = can.Message(
            arbitration_id=can_id_int, data=data_bytes, is_extended_id=is_extended, is_fd=is_fd
        )
        codec.bus.send(msg)
        return {
            "bus": bus,
            "canId": can_id_int,
            "canIdHex": f"0x{can_id_int:x}",
            "dlc": len(data_bytes),
            "dataHex": data_bytes.hex(),
            "isExtended": is_extended,
            "isFd": is_fd,
        }

    @action("Start Periodic Raw", "Start raw periodic transmission. Returns {task_id, replaced}.")
    @action.text("bus", title="Bus")
    @action.text("can_id", title="CAN ID (hex)", placeholder="0x100")
    @action.text("data", title="Data (hex bytes)", placeholder="01 02 03 04")
    @action.number("period_ms", title="Period (ms)", minimum=1, maximum=60_000, default=100)
    @action.boolean("is_extended", title="Extended ID", default=False, widget="toggle")
    @action.boolean("is_fd", title="CAN FD", default=False, widget="toggle")
    def start_periodic_raw(
        self,
        bus: str,
        can_id: str,
        data: str,
        period_ms: int,
        is_extended: bool = False,
        is_fd: bool = False,
    ) -> dict[str, Any]:
        codec = self._codec(bus)
        can_id_int = _parse_can_id(can_id)
        _validate_id_range(can_id_int, is_extended)
        data_bytes = _parse_data_hex(data)
        tid = _task_id(bus, can_id_int, is_extended, "raw")
        replaced = self._stop_slot_if_present(tid)
        msg = can.Message(
            arbitration_id=can_id_int, data=data_bytes, is_extended_id=is_extended, is_fd=is_fd
        )
        self._spawn_periodic(tid, codec, msg, period_ms / 1000.0, mode="raw")
        self._slots[tid] = {
            "taskId": tid,
            "bus": bus,
            "canId": can_id_int,
            "isExtended": is_extended,
            "isFd": is_fd,
            "dlc": len(data_bytes),
            "dataHex": data_bytes.hex(),
            "periodMs": period_ms,
            "mode": "raw",
            "isActive": True,
        }
        return {"task_id": tid, "replaced": replaced}

    # ─── DBC send + periodic ────────────────────────────────────────────

    @action("Send Message", "Send a one-shot DBC-encoded message")
    @action.text("bus", title="Bus")
    @action.text("message", title="DBC message name")
    @action.text("signals_json", title="Signals (JSON object)", placeholder='{"Speed": 50}')
    @action.text("mux", title="Multiplexer (optional)", required=False, default="")
    def send_message(
        self, bus: str, message: str, signals_json: str, mux: str = ""
    ) -> dict[str, Any]:
        codec = self._codec(bus)
        signals = _parse_signals_json(signals_json)
        dbc_msg = _resolve_dbc_message(codec, message)
        mux_value = _parse_mux(mux)
        data_bytes = _encode_dbc(dbc_msg, signals, mux_value)
        msg = can.Message(
            arbitration_id=dbc_msg.frame_id,
            data=data_bytes,
            is_extended_id=dbc_msg.is_extended_frame,
        )
        codec.bus.send(msg)
        return {
            "bus": bus,
            "message": message,
            "canId": dbc_msg.frame_id,
            "canIdHex": f"0x{dbc_msg.frame_id:x}",
            "dlc": len(data_bytes),
            "dataHex": data_bytes.hex(),
            "mux": mux_value,
        }

    @action(
        "Start Periodic Message",
        "Start DBC-encoded periodic transmission. Returns {task_id, replaced}.",
    )
    @action.text("bus", title="Bus")
    @action.text("message", title="DBC message name")
    @action.text("signals_json", title="Signals (JSON object)", placeholder='{"Speed": 50}')
    @action.number("period_ms", title="Period (ms)", minimum=1, maximum=60_000, default=100)
    @action.text("mux", title="Multiplexer (optional)", required=False, default="")
    def start_periodic_message(
        self,
        bus: str,
        message: str,
        signals_json: str,
        period_ms: int,
        mux: str = "",
    ) -> dict[str, Any]:
        codec = self._codec(bus)
        signals = _parse_signals_json(signals_json)
        dbc_msg = _resolve_dbc_message(codec, message)
        mux_value = _parse_mux(mux)
        data_bytes = _encode_dbc(dbc_msg, signals, mux_value)
        mux_key = "dbc" if mux_value is None else f"mux={mux_value}"
        tid = _task_id(bus, dbc_msg.frame_id, dbc_msg.is_extended_frame, mux_key)
        replaced = self._stop_slot_if_present(tid)
        msg = can.Message(
            arbitration_id=dbc_msg.frame_id,
            data=data_bytes,
            is_extended_id=dbc_msg.is_extended_frame,
        )
        self._spawn_periodic(tid, codec, msg, period_ms / 1000.0, mode="dbc")
        self._slots[tid] = {
            "taskId": tid,
            "bus": bus,
            "canId": dbc_msg.frame_id,
            "isExtended": dbc_msg.is_extended_frame,
            "isFd": False,
            "dlc": len(data_bytes),
            "dataHex": data_bytes.hex(),
            "periodMs": period_ms,
            "mode": "dbc",
            "isActive": True,
            "message": {"name": message, "mux": mux_value, "signals": signals},
        }
        return {"task_id": tid, "replaced": replaced}

    # ─── Stop ───────────────────────────────────────────────────────────

    @action("Stop Periodic", "Stop a periodic task by stable task_id")
    @action.text("task_id", title="Task ID")
    def stop_periodic(self, task_id: str) -> dict[str, Any]:
        existed = self._stop_slot_if_present(task_id)
        return {"task_id": task_id, "stopped": existed}

    # ─── Internals ──────────────────────────────────────────────────────

    def _spawn_periodic(
        self, tid: str, codec: CanCodec, msg: can.Message, period_s: float, mode: str
    ) -> None:
        task = asyncio.create_task(self._periodic_loop(tid, codec, msg, period_s))
        self._tasks[tid] = task
        logger.info("started periodic %s mode=%s period=%.3fs", tid, mode, period_s)

    async def _periodic_loop(
        self, tid: str, codec: CanCodec, msg: can.Message, period_s: float
    ) -> None:
        try:
            while codec.running and codec.bus is not None:
                try:
                    codec.bus.send(msg)
                except can.CanError as e:
                    logger.warning("periodic %s send failed: %s", tid, e)
                await asyncio.sleep(period_s)
        except asyncio.CancelledError:
            logger.info("periodic %s cancelled", tid)

    def _stop_slot_if_present(self, tid: str) -> bool:
        task = self._tasks.pop(tid, None)
        self._slots.pop(tid, None)
        if task is None:
            return False
        task.cancel()
        return True

    def stop_all(self) -> None:
        """Cancel every periodic this router owns. Used at extension shutdown."""
        for tid, task in list(self._tasks.items()):
            task.cancel()
            logger.info("shutdown: cancelled periodic %s", tid)
        self._tasks.clear()
        self._slots.clear()

    # Alias for `setup_shutdown_handler` which expects a `.stop()` method.
    stop = stop_all


# ─── Pure helpers — module-level so tests hit them at the helper seam ──────


def _parse_signals_json(raw: str) -> dict[str, Any]:
    if not raw.strip():
        raise ValueError("signals_json must be a JSON object string")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"signals_json is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("signals_json must decode to a JSON object")
    return parsed


def _parse_mux(mux: str) -> int | str | None:
    s = mux.strip()
    if not s:
        return None
    try:
        return int(s, 0)
    except ValueError:
        # Treat as a value-table label; cantools encode resolves it via signal choices.
        return s


def _resolve_dbc_message(codec: CanCodec, message: str) -> cantools.db.can.Message:
    dbc_msg = codec.messages_by_name.get(message)
    if dbc_msg is None:
        available = sorted(codec.messages_by_name.keys())
        raise ValueError(f"unknown DBC message '{message}'. Available: {available[:20]}...")
    return dbc_msg


def _encode_dbc(
    dbc_msg: cantools.db.can.Message,
    signals: dict[str, Any],
    mux_value: int | str | None,
) -> bytes:
    # cantools encode_message picks the right mux variant automatically when the
    # multiplexer signal is present in the signals dict. If the caller passed a
    # standalone `mux` field, inject it under the multiplexer signal name.
    payload = dict(signals)
    if mux_value is not None and dbc_msg.is_multiplexed():
        mux_signal = next((sig for sig in dbc_msg.signals if sig.is_multiplexer), None)
        if mux_signal is not None and mux_signal.name not in payload:
            payload[mux_signal.name] = mux_value
    return bytes(dbc_msg.encode(payload))


def _describe_dbc_message(msg: cantools.db.can.Message) -> dict[str, Any]:
    return {
        "name": msg.name,
        "canId": msg.frame_id,
        "isExtended": msg.is_extended_frame,
        "dlc": msg.length,
        "cycleTimeMs": msg.cycle_time,
        "signals": [_describe_dbc_signal(sig) for sig in msg.signals],
    }


def _describe_dbc_signal(sig: cantools.db.can.Signal) -> dict[str, Any]:
    return {
        "name": sig.name,
        "startBit": sig.start,
        "length": sig.length,
        "byteOrder": "little" if sig.byte_order == "little_endian" else "big",
        "isSigned": sig.is_signed,
        "scale": sig.scale,
        "offset": sig.offset,
        "min": sig.minimum,
        "max": sig.maximum,
        "unit": sig.unit,
        "valueTable": {int(k): str(v) for k, v in sig.choices.items()} if sig.choices else None,
        "muxIndicator": sig.is_multiplexer,
        "muxValue": sig.multiplexer_ids[0] if sig.multiplexer_ids else None,
    }
