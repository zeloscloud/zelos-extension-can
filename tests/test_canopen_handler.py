"""Tests for CANopen protocol handler."""

from unittest.mock import MagicMock, patch

import can
import pytest

from zelos_extension_can.protocols.canopen.handler import CANopenHandler


@pytest.fixture
def mock_source():
    with patch("zelos_sdk.TraceSource") as mock:
        source = mock.return_value
        source.add_event.return_value = MagicMock()
        yield source


@pytest.fixture
def handler(mock_source):
    config = {"canopen": True}
    return CANopenHandler(config, mock_source, namespace=None, bus_name=None)


class TestCANopenRouting:
    """Test frame routing logic."""

    def test_extended_id_returns_false(self, handler):
        """Extended (29-bit) frames are not CANopen."""
        msg = can.Message(arbitration_id=0x18FEF100, is_extended_id=True, data=bytes(8))
        assert handler.handle_frame(msg, None) is False

    def test_heartbeat_consumed(self, handler):
        """Heartbeat frames are consumed."""
        msg = can.Message(arbitration_id=0x701, is_extended_id=False, data=bytes([0x05]))
        assert handler.handle_frame(msg, None) is True

    def test_emergency_consumed(self, handler):
        """EMERGENCY frames are consumed."""
        msg = can.Message(
            arbitration_id=0x081,
            is_extended_id=False,
            data=bytes([0x10, 0x42, 0x04, 0, 0, 0, 0, 0]),
        )
        assert handler.handle_frame(msg, None) is True

    def test_nmt_consumed(self, handler):
        """NMT frames are consumed."""
        msg = can.Message(
            arbitration_id=0x000,
            is_extended_id=False,
            data=bytes([0x01, 0x01]),
        )
        assert handler.handle_frame(msg, None) is True

    def test_sync_consumed(self, handler):
        """SYNC frames are consumed."""
        msg = can.Message(arbitration_id=0x080, is_extended_id=False, data=b"")
        assert handler.handle_frame(msg, None) is True

    def test_pdo_without_eds_passes_through(self, handler):
        """PDO frames without EDS mapping pass through to DBC."""
        msg = can.Message(arbitration_id=0x181, is_extended_id=False, data=bytes(8))
        assert handler.handle_frame(msg, None) is False

    def test_unknown_cob_id_passes_through(self, handler):
        """Unknown COB-IDs pass through to DBC."""
        msg = can.Message(arbitration_id=0x7FF, is_extended_id=False, data=bytes(8))
        assert handler.handle_frame(msg, None) is False


class TestNMTTracking:
    """Test NMT state tracking via handler."""

    def test_tracks_node_states(self, handler):
        """Handler tracks NMT states from heartbeats."""
        msg = can.Message(arbitration_id=0x701, is_extended_id=False, data=bytes([0x05]))
        handler.handle_frame(msg, None)

        states = handler.get_node_states()
        assert states["count"] == 1
        assert "1" in states["nodes"]
        assert states["nodes"]["1"] == "OPERATIONAL"

    def test_boot_to_operational(self, handler):
        """Handler tracks state transitions."""
        # Boot-up
        msg1 = can.Message(arbitration_id=0x701, is_extended_id=False, data=bytes([0x00]))
        handler.handle_frame(msg1, None)
        # Operational
        msg2 = can.Message(arbitration_id=0x701, is_extended_id=False, data=bytes([0x05]))
        handler.handle_frame(msg2, None)

        states = handler.get_node_states()
        assert states["nodes"]["1"] == "OPERATIONAL"


class TestEmergencyTracking:
    """Test EMERGENCY message tracking."""

    def test_emergency_logged(self, handler):
        """Handler logs EMERGENCY messages."""
        msg = can.Message(
            arbitration_id=0x081,
            is_extended_id=False,
            data=bytes([0x10, 0x42, 0x04, 0, 0, 0, 0, 0]),
        )
        handler.handle_frame(msg, None)

        emergencies = handler.get_emergencies()
        assert emergencies["count"] == 1
        assert emergencies["emergencies"][0]["error_code"] == "0x4210"


class TestStatus:
    """Test status and metrics."""

    def test_get_status(self, handler):
        status = handler.get_status()
        assert status["protocol"] == "canopen"
        assert "known_nodes" in status

    def test_get_metrics(self, handler):
        metrics = handler.get_metrics()
        assert "known_nodes" in metrics
        assert "emergency_count" in metrics
