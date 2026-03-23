"""J1939 Transport Protocol state machine for BAM and RTS/CTS reassembly.

Implements passive monitoring of multi-frame J1939 messages per SAE J1939-21.
Supports both Broadcast Announce Message (BAM) and RTS/CTS connection-mode transfers.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import can
import cantools.j1939

from .pgn import PGN_TP_CM, PGN_TP_DT, pgn_from_frame_id

logger = logging.getLogger(__name__)

# TP.CM control byte values
CM_RTS = 16  # Request To Send
CM_CTS = 17  # Clear To Send
CM_END_OF_MSG_ACK = 19  # End of Message Acknowledgment
CM_BAM = 32  # Broadcast Announce Message
CM_ABORT = 255  # Connection Abort


def _pgn_from_cm_data(data: bytes) -> int:
    """Extract the PGN from TP.CM payload bytes 5-7."""
    return data[5] | (data[6] << 8) | (data[7] << 16)


@dataclass
class TPSession:
    """Active transport protocol session."""

    source_address: int
    destination_address: int
    pgn: int
    total_bytes: int
    total_packets: int
    tp_type: str  # "BAM" or "RTS_CTS"
    data: bytearray = field(default_factory=bytearray)
    packets_received: int = 0
    next_sequence: int = 1
    last_activity: float = field(default_factory=time.monotonic)

    @property
    def session_key(self) -> tuple[int, int, int]:
        return (self.source_address, self.destination_address, self.pgn)

    @property
    def is_complete(self) -> bool:
        return self.packets_received >= self.total_packets


class TPStateMachine:
    """J1939 Transport Protocol state machine for passive reassembly."""

    def __init__(
        self,
        on_complete: Callable[[int, int, bytes, str], None],
        timeout_ms: int = 1250,
        max_sessions: int = 64,
    ) -> None:
        self.on_complete = on_complete
        self.timeout_s = timeout_ms / 1000.0
        self.max_sessions = max_sessions
        self._sessions: dict[tuple[int, int, int], TPSession] = {}
        # Reverse index: (sa, da) → session key for O(1) TP.DT lookup
        self._dt_index: dict[tuple[int, int], tuple[int, int, int]] = {}

        self.completed_transfers = 0
        self.aborted_transfers = 0
        self.timed_out_transfers = 0

    def handle_frame(self, msg: can.Message, frame_id: cantools.j1939.FrameId) -> bool:
        """Process a TP.CM or TP.DT frame.

        :return: True (TP frames are always consumed)
        """
        pgn = pgn_from_frame_id(frame_id)

        if pgn == PGN_TP_CM:
            self._handle_tp_cm(msg, frame_id)
        elif pgn == PGN_TP_DT:
            self._handle_tp_dt(msg, frame_id)

        return True

    def cleanup_stale(self) -> int:
        """Remove timed-out sessions."""
        now = time.monotonic()
        stale_keys = [
            key
            for key, session in self._sessions.items()
            if (now - session.last_activity) > self.timeout_s
        ]

        for key in stale_keys:
            self._remove_session(key)
            self.timed_out_transfers += 1

        return len(stale_keys)

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    def _add_session(self, session: TPSession) -> None:
        key = session.session_key
        self._sessions[key] = session
        self._dt_index[(session.source_address, session.destination_address)] = key

    def _remove_session(self, key: tuple[int, int, int]) -> TPSession | None:
        session = self._sessions.pop(key, None)
        if session:
            self._dt_index.pop((session.source_address, session.destination_address), None)
        return session

    def _handle_tp_cm(self, msg: can.Message, frame_id: cantools.j1939.FrameId) -> None:
        if len(msg.data) < 8:
            return

        control_byte = msg.data[0]
        sa = frame_id.source_address

        if control_byte == CM_BAM:
            self._start_session(msg, sa, 0xFF, "BAM")
        elif control_byte == CM_RTS:
            self._start_session(msg, sa, frame_id.pdu_specific, "RTS_CTS")
        elif control_byte == CM_ABORT:
            self._handle_abort(msg, sa, frame_id.pdu_specific)

    def _start_session(
        self, msg: can.Message, source_address: int, destination: int, tp_type: str
    ) -> None:
        total_bytes = msg.data[1] | (msg.data[2] << 8)
        total_packets = msg.data[3]
        pgn = _pgn_from_cm_data(msg.data)

        key = (source_address, destination, pgn)

        if key not in self._sessions and len(self._sessions) >= self.max_sessions:
            logger.warning(
                "TP session cap reached (%d), dropping %s from SA=0x%02X PGN=0x%04X",
                self.max_sessions,
                tp_type,
                source_address,
                pgn,
            )
            return

        self._add_session(
            TPSession(
                source_address=source_address,
                destination_address=destination,
                pgn=pgn,
                total_bytes=total_bytes,
                total_packets=total_packets,
                tp_type=tp_type,
            )
        )

    def _handle_abort(self, msg: can.Message, source_address: int, destination: int) -> None:
        pgn = _pgn_from_cm_data(msg.data)

        for key in [
            (source_address, destination, pgn),
            (destination, source_address, pgn),
        ]:
            if key in self._sessions:
                self._remove_session(key)
                self.aborted_transfers += 1
                return

    def _handle_tp_dt(self, msg: can.Message, frame_id: cantools.j1939.FrameId) -> None:
        if len(msg.data) < 2:
            return

        sequence_number = msg.data[0]
        sa = frame_id.source_address
        da = frame_id.pdu_specific

        # O(1) lookup via reverse index — try broadcast first, then specific DA
        session = None
        for lookup_da in (0xFF, da):
            session_key = self._dt_index.get((sa, lookup_da))
            if session_key:
                session = self._sessions.get(session_key)
                if session:
                    break

        if session is None:
            return

        if sequence_number != session.next_sequence:
            self._remove_session(session.session_key)
            self.aborted_transfers += 1
            return

        session.data.extend(msg.data[1:8])
        session.packets_received += 1
        session.next_sequence += 1
        session.last_activity = time.monotonic()

        if session.is_complete:
            payload = bytes(session.data[: session.total_bytes])
            tp_type = session.tp_type
            session_pgn = session.pgn
            session_sa = session.source_address
            self._remove_session(session.session_key)
            self.completed_transfers += 1
            self.on_complete(session_pgn, session_sa, payload, tp_type)
