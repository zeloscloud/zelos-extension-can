"""Tests for J1939 Transport Protocol state machine."""

import can

from zelos_extension_can.protocols.j1939.pgn import parse_frame_id
from zelos_extension_can.protocols.j1939.transport import TPStateMachine


def _build_tp_cm_bam(sa: int, total_bytes: int, total_packets: int, pgn: int) -> can.Message:
    """Build a TP.CM BAM message."""
    arb_id = (6 << 26) | (0xEC << 16) | (0xFF << 8) | sa
    data = bytes(
        [
            32,  # BAM
            total_bytes & 0xFF,
            (total_bytes >> 8) & 0xFF,
            total_packets,
            0xFF,
            pgn & 0xFF,
            (pgn >> 8) & 0xFF,
            (pgn >> 16) & 0xFF,
        ]
    )
    return can.Message(arbitration_id=arb_id, data=data, is_extended_id=True)


def _build_tp_dt(sa: int, da: int, seq: int, payload: bytes) -> can.Message:
    """Build a TP.DT message."""
    arb_id = (6 << 26) | (0xEB << 16) | (da << 8) | sa
    # Pad to 7 bytes
    padded = payload + bytes([0xFF] * (7 - len(payload)))
    data = bytes([seq]) + padded[:7]
    return can.Message(arbitration_id=arb_id, data=data, is_extended_id=True)


def _build_tp_cm_rts(
    sa: int, da: int, total_bytes: int, total_packets: int, pgn: int
) -> can.Message:
    """Build a TP.CM RTS message."""
    arb_id = (6 << 26) | (0xEC << 16) | (da << 8) | sa
    data = bytes(
        [
            16,  # RTS
            total_bytes & 0xFF,
            (total_bytes >> 8) & 0xFF,
            total_packets,
            0xFF,
            pgn & 0xFF,
            (pgn >> 8) & 0xFF,
            (pgn >> 16) & 0xFF,
        ]
    )
    return can.Message(arbitration_id=arb_id, data=data, is_extended_id=True)


def _build_tp_cm_abort(sa: int, da: int, pgn: int) -> can.Message:
    """Build a TP.CM Abort message."""
    arb_id = (6 << 26) | (0xEC << 16) | (da << 8) | sa
    data = bytes(
        [
            255,  # Abort
            0x01,  # Reason
            0xFF,
            0xFF,
            0xFF,
            pgn & 0xFF,
            (pgn >> 8) & 0xFF,
            (pgn >> 16) & 0xFF,
        ]
    )
    return can.Message(arbitration_id=arb_id, data=data, is_extended_id=True)


class TestBAMTransfer:
    """Test Broadcast Announce Message transfers."""

    def test_complete_bam_exact_boundary(self):
        """Complete BAM with exactly 7-byte payload (1 packet)."""
        completed = []
        tp = TPStateMachine(
            on_complete=lambda pgn, sa, data, tp_type: completed.append((pgn, sa, data, tp_type))
        )

        # BAM: 7 bytes, 1 packet, PGN 0xFECA
        bam = _build_tp_cm_bam(sa=0x00, total_bytes=7, total_packets=1, pgn=0xFECA)
        fid = parse_frame_id(bam.arbitration_id)
        tp.handle_frame(bam, fid)

        # DT seq 1
        dt = _build_tp_dt(sa=0x00, da=0xFF, seq=1, payload=bytes(range(7)))
        fid = parse_frame_id(dt.arbitration_id)
        tp.handle_frame(dt, fid)

        assert len(completed) == 1
        assert completed[0][0] == 0xFECA
        assert completed[0][1] == 0x00
        assert completed[0][2] == bytes(range(7))
        assert completed[0][3] == "BAM"

    def test_complete_bam_with_padding(self):
        """BAM with 10-byte payload (2 packets, last has padding)."""
        completed = []
        tp = TPStateMachine(
            on_complete=lambda pgn, sa, data, tp_type: completed.append((pgn, sa, data, tp_type))
        )

        payload = bytes(range(10))
        bam = _build_tp_cm_bam(sa=0x00, total_bytes=10, total_packets=2, pgn=0xFECA)
        fid = parse_frame_id(bam.arbitration_id)
        tp.handle_frame(bam, fid)

        # DT seq 1: bytes 0-6
        dt1 = _build_tp_dt(sa=0x00, da=0xFF, seq=1, payload=payload[0:7])
        tp.handle_frame(dt1, parse_frame_id(dt1.arbitration_id))

        # DT seq 2: bytes 7-9 + padding
        dt2 = _build_tp_dt(sa=0x00, da=0xFF, seq=2, payload=payload[7:10])
        tp.handle_frame(dt2, parse_frame_id(dt2.arbitration_id))

        assert len(completed) == 1
        assert completed[0][2] == payload  # Trimmed to 10 bytes

    def test_bam_timeout(self):
        """Incomplete BAM should be cleaned up on timeout."""
        tp = TPStateMachine(on_complete=lambda *args: None, timeout_ms=0)

        bam = _build_tp_cm_bam(sa=0x00, total_bytes=14, total_packets=2, pgn=0xFECA)
        tp.handle_frame(bam, parse_frame_id(bam.arbitration_id))

        assert tp.active_session_count == 1

        # Cleanup stale sessions (timeout=0 means immediate)
        cleaned = tp.cleanup_stale()
        assert cleaned == 1
        assert tp.active_session_count == 0
        assert tp.timed_out_transfers == 1

    def test_concurrent_bams_different_sa(self):
        """Concurrent BAMs from different source addresses."""
        completed = []
        tp = TPStateMachine(
            on_complete=lambda pgn, sa, data, tp_type: completed.append((pgn, sa, data, tp_type))
        )

        # Start BAM from SA=0x00
        bam1 = _build_tp_cm_bam(sa=0x00, total_bytes=7, total_packets=1, pgn=0xFECA)
        tp.handle_frame(bam1, parse_frame_id(bam1.arbitration_id))

        # Start BAM from SA=0x21
        bam2 = _build_tp_cm_bam(sa=0x21, total_bytes=7, total_packets=1, pgn=0xFECA)
        tp.handle_frame(bam2, parse_frame_id(bam2.arbitration_id))

        assert tp.active_session_count == 2

        # Complete SA=0x00
        dt1 = _build_tp_dt(sa=0x00, da=0xFF, seq=1, payload=bytes([0xAA] * 7))
        tp.handle_frame(dt1, parse_frame_id(dt1.arbitration_id))

        assert len(completed) == 1
        assert completed[0][1] == 0x00

        # Complete SA=0x21
        dt2 = _build_tp_dt(sa=0x21, da=0xFF, seq=1, payload=bytes([0xBB] * 7))
        tp.handle_frame(dt2, parse_frame_id(dt2.arbitration_id))

        assert len(completed) == 2
        assert completed[1][1] == 0x21


class TestRTSCTSTransfer:
    """Test RTS/CTS connection-mode transfers."""

    def test_complete_rts_cts(self):
        """Complete RTS/CTS transfer."""
        completed = []
        tp = TPStateMachine(
            on_complete=lambda pgn, sa, data, tp_type: completed.append((pgn, sa, data, tp_type))
        )

        rts = _build_tp_cm_rts(sa=0x21, da=0x00, total_bytes=7, total_packets=1, pgn=0xFECA)
        tp.handle_frame(rts, parse_frame_id(rts.arbitration_id))

        assert tp.active_session_count == 1

        dt = _build_tp_dt(sa=0x21, da=0x00, seq=1, payload=bytes(range(7)))
        tp.handle_frame(dt, parse_frame_id(dt.arbitration_id))

        assert len(completed) == 1
        assert completed[0][3] == "RTS_CTS"

    def test_rts_cts_abort(self):
        """RTS/CTS abort mid-transfer."""
        completed = []
        tp = TPStateMachine(
            on_complete=lambda pgn, sa, data, tp_type: completed.append((pgn, sa, data, tp_type))
        )

        rts = _build_tp_cm_rts(sa=0x21, da=0x00, total_bytes=14, total_packets=2, pgn=0xFECA)
        tp.handle_frame(rts, parse_frame_id(rts.arbitration_id))

        # Send first packet
        dt = _build_tp_dt(sa=0x21, da=0x00, seq=1, payload=bytes(range(7)))
        tp.handle_frame(dt, parse_frame_id(dt.arbitration_id))

        # Abort
        abort = _build_tp_cm_abort(sa=0x21, da=0x00, pgn=0xFECA)
        tp.handle_frame(abort, parse_frame_id(abort.arbitration_id))

        assert len(completed) == 0
        assert tp.active_session_count == 0
        assert tp.aborted_transfers == 1


class TestSessionManagement:
    """Test session cap and out-of-order handling."""

    def test_session_cap(self):
        """Session cap prevents new sessions beyond max."""
        tp = TPStateMachine(on_complete=lambda *args: None, max_sessions=2)

        for i in range(3):
            bam = _build_tp_cm_bam(sa=i, total_bytes=7, total_packets=1, pgn=0xFECA)
            tp.handle_frame(bam, parse_frame_id(bam.arbitration_id))

        # Only 2 sessions should exist
        assert tp.active_session_count == 2

    def test_out_of_order_discards_session(self):
        """Out-of-order packets discard the session."""
        completed = []
        tp = TPStateMachine(on_complete=lambda *args: completed.append(args))

        bam = _build_tp_cm_bam(sa=0x00, total_bytes=14, total_packets=2, pgn=0xFECA)
        tp.handle_frame(bam, parse_frame_id(bam.arbitration_id))

        # Send seq=2 before seq=1
        dt2 = _build_tp_dt(sa=0x00, da=0xFF, seq=2, payload=bytes(7))
        tp.handle_frame(dt2, parse_frame_id(dt2.arbitration_id))

        assert tp.active_session_count == 0
        assert len(completed) == 0
        assert tp.aborted_transfers == 1

    def test_stale_cleanup(self):
        """Stale sessions are cleaned up."""
        tp = TPStateMachine(on_complete=lambda *args: None, timeout_ms=0)

        bam = _build_tp_cm_bam(sa=0x00, total_bytes=14, total_packets=2, pgn=0xFECA)
        tp.handle_frame(bam, parse_frame_id(bam.arbitration_id))

        cleaned = tp.cleanup_stale()
        assert cleaned == 1
        assert tp.timed_out_transfers == 1
