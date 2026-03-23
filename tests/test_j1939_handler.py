"""Tests for J1939 protocol handler."""

from unittest.mock import MagicMock, patch

import can
import pytest

from zelos_extension_can.protocols.j1939.handler import J1939Handler


@pytest.fixture
def mock_source():
    with patch("zelos_sdk.TraceSource") as mock:
        source = mock.return_value
        source.add_event.return_value = MagicMock()
        yield source


@pytest.fixture
def handler(mock_source):
    config = {"j1939_enabled": True}
    return J1939Handler(config, mock_source, namespace=None, bus_name=None)


class TestJ1939Routing:
    """Test frame routing logic."""

    def test_non_extended_returns_false(self, handler):
        """Non-extended (11-bit) frames are not J1939."""
        msg = can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes(8))
        assert handler.handle_frame(msg, None) is False

    def test_extended_single_frame_returns_false(self, handler):
        """Single-frame J1939 messages return False (DBC still decodes)."""
        # CCVS PGN 65265
        arb_id = 0x18FEF100
        msg = can.Message(arbitration_id=arb_id, is_extended_id=True, data=bytes(8))
        assert handler.handle_frame(msg, None) is False

    def test_tp_cm_returns_true(self, handler):
        """TP.CM frames are consumed."""
        # TP.CM BAM from SA=0x00
        arb_id = (6 << 26) | (0xEC << 16) | (0xFF << 8) | 0x00
        data = bytes([32, 7, 0, 1, 0xFF, 0xCA, 0xFE, 0x00])  # BAM for DM1
        msg = can.Message(arbitration_id=arb_id, is_extended_id=True, data=data)
        assert handler.handle_frame(msg, None) is True

    def test_tp_dt_returns_true(self, handler):
        """TP.DT frames are consumed."""
        # First set up a BAM session
        bam_arb = (6 << 26) | (0xEC << 16) | (0xFF << 8) | 0x00
        bam_data = bytes([32, 7, 0, 1, 0xFF, 0xCA, 0xFE, 0x00])
        bam_msg = can.Message(arbitration_id=bam_arb, is_extended_id=True, data=bam_data)
        handler.handle_frame(bam_msg, None)

        # Then send TP.DT
        dt_arb = (6 << 26) | (0xEB << 16) | (0xFF << 8) | 0x00
        dt_data = bytes([1, 0, 0, 0, 0, 0, 0, 0])
        msg = can.Message(arbitration_id=dt_arb, is_extended_id=True, data=dt_data)
        assert handler.handle_frame(msg, None) is True


class TestAddressTracking:
    """Test source address tracking."""

    def test_tracks_source_addresses(self, handler):
        """Handler tracks source addresses from received frames."""
        arb_id = 0x18FEF121  # SA=0x21
        msg = can.Message(arbitration_id=arb_id, is_extended_id=True, data=bytes(8))
        handler.handle_frame(msg, None)

        table = handler.get_address_table()
        assert table["count"] == 1
        assert table["addresses"][0]["address"] == "0x21"

    def test_tracks_multiple_pgns(self, handler):
        """Handler tracks unique PGNs per address."""
        # PGN 65265 from SA=0x21
        msg1 = can.Message(arbitration_id=0x18FEF121, is_extended_id=True, data=bytes(8))
        handler.handle_frame(msg1, None)
        # PGN 65266
        msg2 = can.Message(arbitration_id=0x18FEF221, is_extended_id=True, data=bytes(8))
        handler.handle_frame(msg2, None)

        table = handler.get_address_table()
        assert table["addresses"][0]["pgn_count"] == 2


class TestJ1939DBCDecode:
    """Test J1939 DBC decode with PGN-based matching (different source addresses)."""

    @pytest.fixture
    def j1939_codec(self):
        """Create a CanCodec with J1939 DBC and protocol enabled."""
        from pathlib import Path

        from zelos_extension_can.codec import CanCodec

        j1939_dbc = str(Path(__file__).parent / "files" / "test_j1939.dbc")
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "database_file": j1939_dbc,
            "j1939_enabled": True,
        }
        with patch("zelos_sdk.TraceSource"):
            return CanCodec(config)

    def test_pgn_lookup_resolves(self, j1939_codec):
        """PGN-based lookup resolves J1939 messages regardless of source address."""
        # EEC1 in DBC has SA=0xFE, but we look up with SA=0x00
        msg = can.Message(arbitration_id=0x0CF00400, is_extended_id=True, data=bytes(8))
        dbc_msg = j1939_codec._find_dbc_msg(msg)
        assert dbc_msg is not None
        assert dbc_msg.name == "EEC1"

    def test_decode_with_different_source_address(self, j1939_codec):
        """DBC decode works regardless of source address in arbitration ID."""
        # EEC1 DBC has frame_id with SA=0xFE, but real ECU sends from SA=0x00
        # EngineSpeed at bytes 3-4: 0x2000 * 0.125 = 1024 rpm
        data = bytes([0, 0, 0, 0x00, 0x20, 0, 0, 0])

        # SA=0x00 (different from DBC's 0xFE)
        msg_sa00 = can.Message(arbitration_id=0x0CF00400, is_extended_id=True, data=data)
        j1939_codec._handle_message(msg_sa00)
        assert j1939_codec.metrics.messages_decoded >= 1
        assert j1939_codec.metrics.unknown_messages == 0

        # SA=0x21 (yet another SA)
        msg_sa21 = can.Message(arbitration_id=0x0CF00421, is_extended_id=True, data=data)
        j1939_codec._handle_message(msg_sa21)
        assert j1939_codec.metrics.messages_decoded >= 2
        assert j1939_codec.metrics.unknown_messages == 0

    def test_decode_ccvs_from_any_sa(self, j1939_codec):
        """CCVS (PDU2) decodes from any source address."""
        # WheelBasedVehicleSpeed at bytes 1-2: 0x6400 * 0.00390625 = 100 km/h
        data = bytes([0, 0x00, 0x64, 0, 0, 0, 0, 0])
        msg = can.Message(arbitration_id=0x18FEF100, is_extended_id=True, data=data)
        j1939_codec._handle_message(msg)
        assert j1939_codec.metrics.messages_decoded >= 1


class TestStatus:
    """Test status and metrics."""

    def test_get_status(self, handler):
        status = handler.get_status()
        assert status["protocol"] == "j1939"
        assert "active_tp_sessions" in status

    def test_get_metrics(self, handler):
        metrics = handler.get_metrics()
        assert "tp_completed" in metrics
        assert "tracked_addresses" in metrics

    def test_tp_sessions(self, handler):
        sessions = handler.get_tp_sessions()
        assert sessions["active_sessions"] == 0
        assert sessions["completed"] == 0
