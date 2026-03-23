"""Tests for J1939 PGN parsing utilities."""

from zelos_extension_can.protocols.j1939.pgn import (
    PGN_DM1,
    PGN_TP_CM,
    PGN_TP_DT,
    build_arb_id,
    destination_from_frame_id,
    is_transport_frame,
    parse_frame_id,
    pgn_from_frame_id,
)


class TestPGNExtraction:
    """Test PGN extraction from known frame IDs."""

    def test_eec1_pgn(self):
        """EEC1 (PGN 61444 / 0xF004) from SA 0x00."""
        fid = parse_frame_id(0x0CF00400)
        assert pgn_from_frame_id(fid) == 61444

    def test_ccvs_pgn(self):
        """CCVS (PGN 65265 / 0xFEF1) from SA 0x00."""
        fid = parse_frame_id(0x18FEF100)
        assert pgn_from_frame_id(fid) == 65265

    def test_lfe_pgn(self):
        """LFE (PGN 65266 / 0xFEF2) from SA 0x21."""
        fid = parse_frame_id(0x18FEF221)
        assert pgn_from_frame_id(fid) == 65266

    def test_dm1_pgn(self):
        """DM1 (PGN 65226 / 0xFECA) from SA 0x00."""
        fid = parse_frame_id(0x18FECA00)
        assert pgn_from_frame_id(fid) == PGN_DM1

    def test_tp_cm_pgn(self):
        """TP.CM (PGN 0xEC00) from SA 0x21 to DA 0x00."""
        fid = parse_frame_id(0x18EC0021)
        assert pgn_from_frame_id(fid) == PGN_TP_CM

    def test_tp_dt_pgn(self):
        """TP.DT (PGN 0xEB00) from SA 0x21 to DA 0x00."""
        fid = parse_frame_id(0x18EB0021)
        assert pgn_from_frame_id(fid) == PGN_TP_DT


class TestPDUFormat:
    """Test PDU1 vs PDU2 destination extraction."""

    def test_pdu2_destination_is_broadcast(self):
        fid = parse_frame_id(0x18FEF100)
        assert destination_from_frame_id(fid) == 0xFF

    def test_pdu1_destination_is_specific(self):
        fid = parse_frame_id(0x18EC0021)  # DA=0x00, SA=0x21
        assert destination_from_frame_id(fid) == 0x00


class TestFrameIdParsing:
    """Test frame ID unpacking."""

    def test_priority(self):
        fid = parse_frame_id(0x18FEF100)
        assert fid.priority == 6

    def test_high_priority(self):
        fid = parse_frame_id(0x0CF00400)
        assert fid.priority == 3

    def test_source_address_field(self):
        fid = parse_frame_id(0x18FEF121)
        assert fid.source_address == 0x21

    def test_pdu_format_field(self):
        fid = parse_frame_id(0x18FEF100)
        assert fid.pdu_format == 0xFE


class TestBuildArbId:
    """Test arbitration ID construction."""

    def test_roundtrip_pdu2(self):
        """build_arb_id -> parse_frame_id -> pgn_from_frame_id roundtrip."""
        arb_id = build_arb_id(pgn=65265, source_address=0x21, priority=6)
        fid = parse_frame_id(arb_id)
        assert pgn_from_frame_id(fid) == 65265
        assert fid.source_address == 0x21
        assert fid.priority == 6

    def test_roundtrip_pdu1(self):
        arb_id = build_arb_id(pgn=PGN_TP_CM, source_address=0x00, priority=6)
        fid = parse_frame_id(arb_id)
        assert pgn_from_frame_id(fid) == PGN_TP_CM


class TestTransportFrameDetection:
    """Test transport protocol frame detection."""

    def test_tp_cm_detected(self):
        assert is_transport_frame(PGN_TP_CM)

    def test_tp_dt_detected(self):
        assert is_transport_frame(PGN_TP_DT)

    def test_normal_pgn_not_transport(self):
        assert not is_transport_frame(65265)

    def test_dm1_not_transport(self):
        assert not is_transport_frame(PGN_DM1)
