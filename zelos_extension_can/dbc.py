"""DBC files: loading and merging a bus's ordered DBC list, and the shapes
the actions describe and encode messages with."""

import hashlib
import logging
import math
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any

import cantools

from .utils.file_utils import resolve_database_file

logger = logging.getLogger(__name__)


def encode_dbc(
    dbc_msg: cantools.database.can.Message,
    signals: dict[str, Any],
    mux_value: int | str | None,
) -> bytes:
    # cantools encode_message picks the right mux variant when the multiplexer
    # signal is present in the input. If the caller passed a standalone `mux`
    # field, inject it under the multiplexer signal name.
    payload = dict(signals)
    mux_signal = next((sig for sig in dbc_msg.signals if sig.is_multiplexer), None)
    if mux_value is not None and mux_signal is not None and mux_signal.name not in payload:
        payload[mux_signal.name] = mux_value
    # strict=False lets authors send sentinel / SNA values that fall outside
    # the DBC's declared [min|max] but still fit the signal's bit field
    # (common pattern: raw 0xFF on an 8-bit field to mark "signal not
    # available"). The bit-field range itself is still enforced by cantools;
    # the webapp does an additional pre-flight check against the bit-field
    # range so out-of-bits values are caught before they reach us.
    try:
        return bytes(dbc_msg.encode(payload, strict=False))
    except KeyError as e:
        # cantools raises a bare KeyError naming the first missing signal; name
        # every one, and for a mux only the selected variant's plus the base.
        selected = payload.get(mux_signal.name) if mux_signal is not None else None
        missing = [
            sig.name
            for sig in dbc_msg.signals
            if sig.name not in payload
            and (sig.multiplexer_ids is None or selected in sig.multiplexer_ids)
        ]
        raise ValueError(
            f"message '{dbc_msg.name}' needs signals: {', '.join(missing) or e}"
        ) from e


def describe_dbc_message_summary(msg: cantools.database.can.Message) -> dict[str, Any]:
    """Lightweight identifier-only shape returned by list_messages. Drops the
    signal array so the catalog fetch stays cheap even on multi-thousand-
    message DBCs. The webapp fetches per-message detail via describe_message
    when a specific message is picked."""
    return {
        "name": msg.name,
        "can_id": int(msg.frame_id),
        "is_extended": bool(msg.is_extended_frame),
        "dlc": int(msg.length),
        "cycle_time_ms": msg.cycle_time,
    }


def describe_dbc_message(msg: cantools.database.can.Message) -> dict[str, Any]:
    return {
        **describe_dbc_message_summary(msg),
        "signals": [describe_dbc_signal(sig) for sig in msg.signals],
    }


def hash_dbc_file(path: Path) -> str:
    """Cache-busting fingerprint for one DBC — SHA1 of its bytes, truncated to
    16 hex chars. Collision risk is irrelevant: the field is purely a
    same-vs-different signal the webapp keys its React Query by."""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:16]


def bus_database_files(bus_config: dict[str, Any]) -> list[str]:
    """A bus config's DBC list, in precedence order.

    A pre-list config carries one `database_file`; it takes precedence, so it
    is prepended to any list.
    """
    files = [str(p) for p in (bus_config.get("database_files") or [])]
    legacy = bus_config.get("database_file")
    return [str(legacy), *files] if legacy else files


def definition_key(msg: cantools.database.can.Message) -> tuple[int, bool, str]:
    """Identity of ONE surviving definition. Several DBCs may define a single
    frame id under different names; each keeps its own table, schema cache slot
    and error blocklist entry."""
    return (msg.frame_id, msg.is_extended_frame, msg.name)


def _report_unpaired(
    defined: dict[tuple[int, bool, str], Path],
    survivors: AbstractSet[tuple[int, bool, str]],
) -> None:
    """Log every definition the two parsers failed to pair, and keep going.

    Pairing is by (id, extended, name), so a definition the two parsers name
    differently matches nothing — a DBC-attribute rename cantools applies and
    the Rust parser does not (`SystemMessageLongSymbol` was one) does exactly
    that. Such a definition decodes under the decoder's name but is absent
    from TX and describe, so say so on both sides rather than drop it
    silently. A conflict or an identical duplicate shares its key with its
    winner and is NOT reported here. Nothing fails the bus.
    """
    for frame_id, is_extended, name in sorted(defined.keys() - survivors):
        logger.error(
            "DBC definition 0x%x (extended=%s) '%s' from %s pairs with no decoder definition "
            "of that name; it will not be addressable for transmit or describe",
            frame_id,
            is_extended,
            name,
            defined[(frame_id, is_extended, name)],
        )
    for frame_id, is_extended, name in sorted(survivors - defined.keys()):
        at_id = ", ".join(
            f"'{n}' in {p.name}"
            for (i, e, n), p in defined.items()
            if (i, e) == (frame_id, is_extended)
        )
        logger.error(
            "decoder definition 0x%x (extended=%s) '%s' pairs with no DBC definition of that "
            "name (the files define %s at that id); it decodes but cannot be addressed by name",
            frame_id,
            is_extended,
            name,
            at_id or "nothing",
        )


def _merge_dbcs(
    files: Sequence[Path],
    databases: Sequence[cantools.database.can.Database],
) -> tuple[
    list[cantools.database.can.Message],
    dict[tuple[int, bool, str], Path],
    dict[tuple[int, bool, str], str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[int],
]:
    """Merge an ordered DBC list, deferring to the Rust decoder's rule.

    A bus-less `zelos_can.CanDecoder` parses exactly the list the Rust codec
    would — no socket, no trace — and reports which definitions survived. The
    rule lives there and nowhere else: a hand-mirrored copy here only bought
    two ways to disagree about what `dbc_conflicts` means.

    Two definitions of one frame id under DIFFERENT names both survive (an
    overlap) and a matching frame decodes under each. Only the same id under the
    same name, laid out differently, is a conflict; the later file wins it.

    :return: (surviving cantools messages, origin file per definition, message
        key per definition, conflict records, overlap records, per-file message
        counts)
    """
    import zelos_can

    decoder = zelos_can.CanDecoder(database_file=[str(p) for p in files] or None)

    # The decoder names survivors, not objects; pair each back to the cantools
    # Message the encode / describe paths need. Event names are
    # `{frame_id:0{4,8}x}_{name}`, one per surviving definition, and both sides
    # read the name verbatim off the same `BO_` line, so the match is exact.
    # That event name IS the message key: taken from here, never re-derived.
    survivors = {
        (frame_id, is_extended, event_name.split("_", 1)[1]): event_name
        for frame_id, is_extended, event_name in decoder.message_keys()
    }

    # Walk the files in list order, each in definition order. A redefinition
    # keeps the position it first appeared at, so the list stays stable as files
    # are appended.
    kept: dict[tuple[int, bool, str], cantools.database.can.Message] = {}
    origin: dict[tuple[int, bool, str], Path] = {}
    defined: dict[tuple[int, bool, str], Path] = {}
    for path, db in zip(files, databases, strict=True):
        for msg in db.messages:
            key = definition_key(msg)
            defined[key] = path
            if key not in survivors:
                continue
            kept[key] = msg
            origin[key] = path

    messages = list(kept.values())
    keys = {key: survivors[key] for key in kept}
    _report_unpaired(defined, survivors.keys())

    conflicts: list[dict[str, Any]] = []
    for record in decoder.dbc_conflicts():
        winner, dropped = record["kept"], record["dropped"]
        logger.warning(
            "conflicting definitions of CAN id 0x%x (extended=%s): '%s' from %s vs '%s' from %s "
            "- keeping '%s'",
            record["frame_id"],
            record["is_extended"],
            dropped["name"],
            dropped["source"],
            winner["name"],
            winner["source"],
            winner["name"],
        )
        conflicts.append(
            {
                "frame_id": record["frame_id"],
                "is_extended": record["is_extended"],
                "kept": {"file": Path(winner["source"]).name, "name": winner["name"]},
                "dropped": {"file": Path(dropped["source"]).name, "name": dropped["name"]},
            }
        )

    overlaps: list[dict[str, Any]] = []
    for record in decoder.dbc_overlaps():
        logger.info(
            "CAN id 0x%x (extended=%s) is defined %d times, each decoded into its own table: %s",
            record["frame_id"],
            record["is_extended"],
            len(record["names"]),
            ", ".join(f"'{n['name']}' from {n['source']}" for n in record["names"]),
        )
        overlaps.append(
            {
                "frame_id": record["frame_id"],
                "is_extended": record["is_extended"],
                "names": [
                    {"file": Path(n["source"]).name, "name": n["name"]} for n in record["names"]
                ],
            }
        )

    counts = [db["message_count"] for db in decoder.databases()]
    return messages, origin, keys, conflicts, overlaps, counts


def describe_dbc_signal(sig: cantools.database.can.Signal) -> dict[str, Any]:
    # JSON requires string dict keys, and the wire encoder rejects Decimal —
    # coerce scale/offset/min/max to float and value_table keys to str.
    scale = float(sig.scale) if sig.scale is not None else 1.0
    offset = float(sig.offset) if sig.offset is not None else 0.0
    return {
        "name": sig.name,
        "start_bit": int(sig.start),
        "length": int(sig.length),
        "byte_order": "little" if sig.byte_order == "little_endian" else "big",
        "is_signed": bool(sig.is_signed),
        "scale": scale,
        "offset": offset,
        "min": float(sig.minimum) if sig.minimum is not None else None,
        "max": float(sig.maximum) if sig.maximum is not None else None,
        "unit": sig.unit,
        "value_table": _physical_value_table(sig, scale, offset),
        "mux_indicator": bool(sig.is_multiplexer),
        "mux_value": int(sig.multiplexer_ids[0]) if sig.multiplexer_ids else None,
    }


def scale_precision(scale: float) -> int:
    """Decimal places implied by a signal's scale. scale=0.001 → 3,
    scale=0.1 → 1, scale=1 → 0, scale=10 → 0 (no fractional precision).
    Used to trim fp64 noise out of decoded physical values so they
    string-match the value_table keys produced by `_physical_value_table`
    and so the trace shows the same precision the wire actually carries."""
    if not scale or scale <= 0 or scale >= 1:
        return 0
    return max(0, -math.floor(math.log10(scale)))


def _physical_value_table(
    sig: cantools.database.can.Signal, scale: float, offset: float
) -> dict[str, str] | None:
    """JSON-wire form of the physical value table, used by describe_message.

    See `value_table_for_trace` for the in-process float/int dict form used
    by zelos-sdk's `add_value_table`. Both must agree on the physical key so
    a value emitted to the trace matches the value-table entry exactly."""
    numeric = value_table_for_trace(sig)
    if numeric is None:
        return None
    return {format(k, ".10g") if isinstance(k, float) else str(k): v for k, v in numeric.items()}


def value_table_for_trace(
    sig: cantools.database.can.Signal,
) -> dict[int | float, str] | None:
    """Build a value table keyed on the physical (scaled+offset) value, so
    trace consumers' lookups match the values we actually emit.

    DBC `VAL_` entries map RAW integer values to labels by convention. For
    enum signals (scale=1, offset=0) the raw int IS the physical value, so
    we use int keys. For scaled signals (e.g. cell_voltage with scale 0.001)
    the physical value is float; we convert and round to the scale's
    precision so the key matches the value `_convert_signals` will emit
    (which is also `round(decoded, precision)`)."""
    if not sig.choices:
        return None
    scale = float(sig.scale) if sig.scale is not None else 1.0
    offset = float(sig.offset) if sig.offset is not None else 0.0
    precision = scale_precision(scale)
    out: dict[int | float, str] = {}
    for raw_int, label in sig.choices.items():
        if scale == 1.0 and offset == 0.0:
            out[int(raw_int)] = str(label)
        else:
            physical = int(raw_int) * scale + offset
            key = round(physical, precision) if precision > 0 else physical
            out[key] = str(label)
    return out


class DbcCatalog:
    """Every message a bus decodes and addresses, merged once from its ordered
    DBC list. Order is precedence; zero files is a legal raw-only bus (nothing
    decodes, raw frames still land)."""

    def __init__(self, files: Sequence[str | Path]) -> None:
        self.database_files: list[Path] = [resolve_database_file(p) for p in files]

        # Each file is loaded on its own (never `add_dbc_file`) so the merge
        # below owns precedence and reports what it did.
        self.databases: list[cantools.database.can.Database] = []
        for path in self.database_files:
            logger.info("Loading CAN database file: %s", path)
            try:
                self.databases.append(cantools.database.load_file(str(path)))
            except Exception as e:
                raise ValueError(f"Failed to load database file: {e}") from e

        (
            self.messages,
            self.message_origin,
            self.message_keys,
            self.dbc_conflicts,
            self.dbc_overlaps,
            counts,
        ) = _merge_dbcs(self.database_files, self.databases)
        # Every definition the merge dropped was either an identical duplicate
        # or a reported conflict; an overlap drops nothing.
        logger.info(
            "DBC merge: %d files, %d messages, %d identical duplicates deduped, %d conflicts, "
            "%d overlaps",
            len(self.database_files),
            len(self.messages),
            sum(counts) - len(self.messages) - len(self.dbc_conflicts),
            len(self.dbc_conflicts),
            len(self.dbc_overlaps),
        )

        # Hashed once: `get_tx_state` polls at 1 Hz and must not re-read DBCs.
        self.dbc_entries: list[dict[str, Any]] = [
            {
                "path": str(path),
                "name": path.name,
                "hash": hash_dbc_file(path),
                "message_count": count,
            }
            for path, count in zip(self.database_files, counts, strict=True)
        ]
        # Fingerprint of the list: the per-file digests in order, so reordering
        # the list flips it.
        self.dbc_hash = hashlib.sha1(
            "".join(entry["hash"] for entry in self.dbc_entries).encode()
        ).hexdigest()[:16]

        # An id carries every definition of it, in definition order, because a
        # frame decodes under all of them. `messages_by_key` is the addressing
        # table: one entry per definition, keyed by the trace event name.
        # `messages_by_name` is a last-wins name index the demo simulation
        # reads; TX never resolves through it.
        self.messages_by_id: dict[tuple[int, bool], list[cantools.database.can.Message]] = {}
        self.messages_by_key: dict[str, cantools.database.can.Message] = {}
        self.messages_by_name: dict[str, cantools.database.can.Message] = {}
        self.keys_by_name: dict[str, list[str]] = {}
        for msg in self.messages:
            self.messages_by_id.setdefault((msg.frame_id, msg.is_extended_frame), []).append(msg)
            key = self.key_of(msg)
            self.messages_by_key[key] = msg
            self.messages_by_name[msg.name] = msg
            self.keys_by_name.setdefault(msg.name, []).append(key)

        for name, keys in self.keys_by_name.items():
            if len(keys) > 1:
                logger.warning(
                    "Message name '%s' is defined at %d ids (%s); address each one by its key",
                    name,
                    len(keys),
                    ", ".join(sorted(keys)),
                )

    def key_of(self, msg: cantools.database.can.Message) -> str:
        """This definition's message key: `{frame_id:04x}_{name}` (8 hex digits
        for an extended id), as the decoder spelled it."""
        return self.message_keys[definition_key(msg)]

    def resolve(self, message: str) -> cantools.database.can.Message:
        """A message key, or a name only one definition carries. A name at
        several ids refuses rather than picking one."""
        dbc_msg = self.messages_by_key.get(message)
        if dbc_msg is not None:
            return dbc_msg
        keys = self.keys_by_name.get(message, [])
        if len(keys) == 1:
            return self.messages_by_key[keys[0]]
        if keys:
            raise ValueError(
                f"message '{message}' is defined at several ids; "
                f"use a key: {', '.join(sorted(keys))}"
            )
        preview = sorted(self.messages_by_key)[:20]
        raise ValueError(f"unknown DBC message '{message}'. First 20 available: {preview}")

    @property
    def first_file(self) -> Path | None:
        """First configured DBC, or None on a raw-only bus. Backs the legacy
        single-DBC fields the tx webapp still reads."""
        return self.database_files[0] if self.database_files else None

    def database_of(self, msg: cantools.database.can.Message) -> str | None:
        """Name of the file the merge took this definition from."""
        origin = self.message_origin.get(definition_key(msg))
        return origin.name if origin else None
