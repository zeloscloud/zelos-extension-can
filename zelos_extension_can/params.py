"""Parsing and validation of CAN action inputs, and the ids periodic tasks take."""

import json
from typing import Any


def parse_can_id(can_id: str) -> int:
    """Accept `0x100`, `100`, or hex without prefix; always parse as hex."""
    return int(can_id.strip(), 16)


def parse_data_hex(data: str) -> bytes:
    return bytes.fromhex(data.replace(" ", "").replace(",", ""))


def validate_id_range(can_id: int, is_extended: bool) -> None:
    max_id = 0x1FFFFFFF if is_extended else 0x7FF
    if can_id < 0 or can_id > max_id:
        kind = "extended" if is_extended else "standard"
        raise ValueError(f"can_id 0x{can_id:x} out of range for {kind} ID (max 0x{max_id:x})")


def raw_slot(can_id: int, is_extended: bool) -> str:
    """Slot a raw periodic occupies: arbitration ID + frame kind."""
    return f"0x{can_id:x}:{'ext' if is_extended else 'std'}"


def periodic_task_id(slot: str, mux: str = "raw") -> str:
    """Stable taskId within a single codec — the message key (DBC) or
    `raw_slot` (raw), plus a discriminator.

    Starting a periodic with the same key replaces the existing slot and signals
    `replaced: True` to the caller. Matches the SocketCAN BCM kernel behavior
    (TX_SETUP on the same can_id replaces the existing slot).
    """
    return f"{slot}:{mux}"


def parse_signals_json(raw: str) -> dict[str, Any]:
    if not raw.strip():
        raise ValueError("signals_json must be a JSON object string")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"signals_json is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("signals_json must decode to a JSON object")
    return parsed


def parse_mux(mux: str) -> int | str | None:
    s = mux.strip()
    if not s:
        return None
    try:
        return int(s, 0)
    except ValueError:
        return s
