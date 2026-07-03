"""Pure, total, no-I/O helpers for the ssh-socketcan transport.

Everything here operates on plain values so it can be unit-tested at the
helper seam without spawning ssh, candump, or cansend:

  * :func:`parse_ssh_channel` splits a ``[user@]host:iface`` channel string.
  * :func:`parse_candump_line` turns one ``candump -L`` log line into a
    :class:`ParsedFrame` — **TOTAL**: it never raises, returning ``None`` on
    anything malformed.
  * :func:`format_cansend_frame` renders a ``zelos_can.Message`` (from
    ``ExternalBus.next_tx``) into a ``cansend`` argument string.

No module here imports ``zelos_can`` — :func:`format_cansend_frame` duck-types
its argument so the parsing/formatting layer stays dependency-free and pure.
"""

import re
from typing import NamedTuple

import can.exceptions

# SocketCAN error frames carry the CAN_ERR_FLAG in the (extended) id field.
_CAN_ERR_FLAG = 0x20000000
# Mask off flag bits to recover the plain 29-bit arbitration id.
_CAN_EFF_MASK = 0x1FFFFFFF
# Arbitration-id width masks (standard 11-bit, extended 29-bit).
_SFF_MASK = 0x7FF
_EFF_MASK = 0x1FFFFFFF

# CAN FD flag nibble bits as printed by candump -L after ``##``.
_FD_BRS = 0x01
_FD_ESI = 0x02

_MAX_CLASSIC_LEN = 8
_MAX_FD_LEN = 64

# ssh host / user allow-list. Anchored, and the FIRST character must be
# alphanumeric so a value can never begin with '-'. This is the ssh
# option-injection guard: host/user are appended to the ssh argv, and a token
# like "-oProxyCommand=..." would otherwise be parsed by ssh as an option,
# yielding local command execution from shared bus config. '_' is kept for
# ~/.ssh/config host aliases; '.' and '-' cover FQDNs, IPv4, and hostnames.
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ParsedFrame(NamedTuple):
    """One decoded candump frame, as primitives ready for ``bus.inject``."""

    arb_id: int
    data: bytes
    timestamp: float | None
    is_extended: bool
    is_fd: bool
    is_remote: bool
    is_error: bool
    brs: bool
    esi: bool


def parse_ssh_channel(channel: str) -> tuple[str | None, str, str]:
    """Split ``[user@]host:iface`` into ``(user, host, iface)``.

    ``user`` is ``None`` when no ``user@`` prefix is present. The interface is
    taken as everything after the *last* colon (``rpartition``) so hostnames
    themselves stay intact; an IPv6 literal must be reached via
    ``ssh_extra_opts``/``~/.ssh/config`` rather than embedded here.

    Host and user are validated against :data:`_HOST_RE` (alphanumeric first
    char, then ``[A-Za-z0-9._-]``). This is a security guard, not just
    hygiene: both are appended to the ssh argv, so a value beginning with
    ``-`` (e.g. ``-oProxyCommand=touch /tmp/pwned``) would be parsed by ssh as
    an option and run a local command — reachable from shared bus config.

    :raises can.exceptions.CanInitializationError: if there is no ``:``, the
        host or interface segment is empty, or the host/user contains
        characters outside the allow-list (including a leading ``-``).
    """
    host_part, sep, iface = channel.rpartition(":")
    if not sep:
        raise can.exceptions.CanInitializationError(
            f"invalid ssh-socketcan channel {channel!r}: expected '[user@]host:iface'"
        )
    user_part, at, host = host_part.partition("@")
    if at:
        user: str | None = user_part
    else:
        user, host = None, host_part
    if not host:
        raise can.exceptions.CanInitializationError(
            f"invalid ssh-socketcan channel {channel!r}: empty host"
        )
    if not _HOST_RE.match(host):
        raise can.exceptions.CanInitializationError(
            f"invalid ssh-socketcan host {host!r}: must match {_HOST_RE.pattern} "
            "(alphanumeric first char; no leading '-', which ssh reads as an option)"
        )
    if user is not None and not _HOST_RE.match(user):
        raise can.exceptions.CanInitializationError(
            f"invalid ssh-socketcan user {user!r}: must match {_HOST_RE.pattern} "
            "(alphanumeric first char; no leading '-', which ssh reads as an option)"
        )
    if not iface:
        raise can.exceptions.CanInitializationError(
            f"invalid ssh-socketcan channel {channel!r}: empty interface"
        )
    return user, host, iface


def parse_candump_line(line: bytes) -> ParsedFrame | None:
    """Parse one ``candump -L`` log line; TOTAL — never raises.

    Log format is ``(<ts>) <iface> <id>#<payload>``. Discriminators:

      * id-hex length 3 → standard (11-bit) frame.
      * id-hex length 8 with ``CAN_ERR_FLAG`` (``0x20000000``) → error frame
        (``arb = v & 0x1FFFFFFF``).
      * id-hex length 8 otherwise → extended (29-bit) frame.
      * payload ``#…`` (i.e. ``id##…``) → CAN FD; the first hex nibble after
        ``##`` is the flag field (BRS ``0x01``, ESI ``0x02``), the rest is data.
      * payload starting ``R`` → remote frame (RTR); data is empty and the
        optional dlc suffix is ignored.
      * otherwise → classic data frame (hex payload).

    Rejects (→ ``None``): classic payload > 8 bytes, FD payload > 64 bytes,
    malformed hex/timestamp/id, or a line without the three expected tokens.

    :param line: One raw log line (no trailing newline required).
    :return: The decoded frame, or ``None`` if the line is malformed.
    """
    try:
        text = line.decode("utf-8").strip()
        if not text:
            return None
        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            return None
        ts_tok, _iface, token = parts

        if not (ts_tok.startswith("(") and ts_tok.endswith(")")):
            return None
        timestamp = float(ts_tok[1:-1])

        id_hex, hash_sep, payload = token.partition("#")
        if not hash_sep:
            return None

        v = int(id_hex, 16)
        if len(id_hex) == 3:
            is_extended, is_error, arb = False, False, v
        elif len(id_hex) == 8:
            if v & _CAN_ERR_FLAG:
                is_extended, is_error, arb = False, True, v & _CAN_EFF_MASK
            else:
                is_extended, is_error, arb = True, False, v & _CAN_EFF_MASK
        else:
            return None

        # CAN FD: payload is a second '#' then a flag nibble then hex data.
        if payload.startswith("#"):
            fd_body = payload[1:]
            if not fd_body:
                return None
            flags = int(fd_body[0], 16)
            data = bytes.fromhex(fd_body[1:])
            if len(data) > _MAX_FD_LEN:
                return None
            return ParsedFrame(
                arb_id=arb,
                data=data,
                timestamp=timestamp,
                is_extended=is_extended,
                is_fd=True,
                is_remote=False,
                is_error=is_error,
                brs=bool(flags & _FD_BRS),
                esi=bool(flags & _FD_ESI),
            )

        # Remote frame: 'R' optionally followed by a dlc we ignore.
        if payload.startswith("R"):
            return ParsedFrame(
                arb_id=arb,
                data=b"",
                timestamp=timestamp,
                is_extended=is_extended,
                is_fd=False,
                is_remote=True,
                is_error=is_error,
                brs=False,
                esi=False,
            )

        # Classic data frame.
        data = bytes.fromhex(payload)
        if len(data) > _MAX_CLASSIC_LEN:
            return None
        return ParsedFrame(
            arb_id=arb,
            data=data,
            timestamp=timestamp,
            is_extended=is_extended,
            is_fd=False,
            is_remote=False,
            is_error=is_error,
            brs=False,
            esi=False,
        )
    except Exception:
        return None


def format_cansend_frame(msg) -> str:  # noqa: ANN001 — duck-typed zelos_can.Message
    """Render a ``zelos_can.Message`` into a ``cansend`` argument string.

    The id width comes from the *extended* flag (``08X`` vs ``03X``), never
    from the id's magnitude, so a small extended id still renders 8 wide.
    Remote frames render ``{id}#R``; CAN FD renders ``{id}##{flags:X}{hex}``
    with the flag nibble carrying BRS (``0x01``) and ESI (``0x02``); classic
    frames render ``{id}#{hex}``.

    The id is masked to its width (``0x7FF`` standard / ``0x1FFFFFFF``
    extended) so an out-of-range id can never widen the field beyond what
    ``cansend`` accepts — an over-wide token would be silently rejected on the
    remote, losing the TX frame. Masking keeps this total.

    :param msg: A message with ``arbitration_id``, ``data``, ``is_extended_id``,
        ``is_remote_frame``, ``is_fd``, ``bitrate_switch``,
        ``error_state_indicator`` attributes (e.g. from ``next_tx``).
    :return: The ``cansend`` frame string.
    """
    if msg.is_extended_id:
        id_str = f"{msg.arbitration_id & _EFF_MASK:08X}"
    else:
        id_str = f"{msg.arbitration_id & _SFF_MASK:03X}"
    if msg.is_remote_frame:
        return f"{id_str}#R"
    if msg.is_fd:
        flags = (_FD_BRS if msg.bitrate_switch else 0) | (
            _FD_ESI if msg.error_state_indicator else 0
        )
        return f"{id_str}##{flags:X}{bytes(msg.data).hex().upper()}"
    return f"{id_str}#{bytes(msg.data).hex().upper()}"
