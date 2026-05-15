"""J1939 PGN (Parameter Group Number) parsing utilities.

Uses cantools.j1939 for frame ID unpacking. Provides pure-function
PGN extraction, source address parsing, and transport protocol detection.
"""

import cantools.j1939

# Well-known PGNs
PGN_TP_CM = 0xEC00  # Transport Protocol - Connection Management
PGN_TP_DT = 0xEB00  # Transport Protocol - Data Transfer
PGN_DM1 = 0xFECA  # Active Diagnostic Trouble Codes
PGN_DM2 = 0xFECB  # Previously Active DTCs


def parse_frame_id(arbitration_id: int) -> cantools.j1939.FrameId:
    """Unpack a 29-bit J1939 frame ID into its components."""
    return cantools.j1939.frame_id_unpack(arbitration_id)


def pgn_from_frame_id(frame_id: cantools.j1939.FrameId) -> int:
    """Compute PGN from an already-unpacked FrameId (avoids re-unpacking)."""
    if frame_id.pdu_format < 240:
        return (frame_id.data_page << 16) | (frame_id.pdu_format << 8)
    return (frame_id.data_page << 16) | (frame_id.pdu_format << 8) | frame_id.pdu_specific


def destination_from_frame_id(frame_id: cantools.j1939.FrameId) -> int:
    """Get destination from an already-unpacked FrameId (0xFF for broadcast/PDU2)."""
    if frame_id.pdu_format < 240:
        return frame_id.pdu_specific
    return 0xFF


def build_arb_id(pgn: int, source_address: int, priority: int = 6) -> int:
    """Build a 29-bit J1939 arbitration ID from PGN and source address."""
    pdu_format = (pgn >> 8) & 0xFF
    pdu_specific = pgn & 0xFF
    return (priority << 26) | (pdu_format << 16) | (pdu_specific << 8) | source_address


def is_transport_frame(pgn: int) -> bool:
    """Check if a PGN is a transport protocol frame (TP.CM or TP.DT)."""
    return pgn in (PGN_TP_CM, PGN_TP_DT)
