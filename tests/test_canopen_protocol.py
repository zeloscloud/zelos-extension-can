"""Tests for CANopen protocol utilities."""

from zelos_extension_can.protocols.canopen.protocol import (
    FunctionCode,
    NMTMonitor,
    decode_emergency,
    parse_cob_id,
)


class TestCOBIDParsing:
    """Test COB-ID classification."""

    def test_nmt_command(self):
        func, node_id = parse_cob_id(0x000)
        assert func == FunctionCode.NMT
        assert node_id == 0

    def test_sync(self):
        func, node_id = parse_cob_id(0x080)
        assert func == FunctionCode.SYNC
        assert node_id == 0

    def test_heartbeat(self):
        func, node_id = parse_cob_id(0x701)
        assert func == FunctionCode.HEARTBEAT
        assert node_id == 1

    def test_heartbeat_node_127(self):
        func, node_id = parse_cob_id(0x77F)
        assert func == FunctionCode.HEARTBEAT
        assert node_id == 127

    def test_emergency(self):
        func, node_id = parse_cob_id(0x081)
        assert func == FunctionCode.EMERGENCY
        assert node_id == 1

    def test_tpdo1(self):
        func, node_id = parse_cob_id(0x181)
        assert func == FunctionCode.TPDO1
        assert node_id == 1

    def test_rpdo1(self):
        func, node_id = parse_cob_id(0x201)
        assert func == FunctionCode.RPDO1
        assert node_id == 1

    def test_tpdo2(self):
        func, node_id = parse_cob_id(0x281)
        assert func == FunctionCode.TPDO2
        assert node_id == 1

    def test_sdo_tx(self):
        func, node_id = parse_cob_id(0x581)
        assert func == FunctionCode.SDO_TX
        assert node_id == 1

    def test_sdo_rx(self):
        func, node_id = parse_cob_id(0x601)
        assert func == FunctionCode.SDO_RX
        assert node_id == 1

    def test_unknown_cob_id(self):
        func, node_id = parse_cob_id(0x7FF)
        assert func is None
        assert node_id == 0

    def test_tpdo3(self):
        func, node_id = parse_cob_id(0x381)
        assert func == FunctionCode.TPDO3
        assert node_id == 1

    def test_tpdo4(self):
        func, node_id = parse_cob_id(0x481)
        assert func == FunctionCode.TPDO4
        assert node_id == 1


class TestNMTMonitor:
    """Test NMT state tracking."""

    def test_boot_up_detection(self):
        nmt = NMTMonitor()
        state = nmt.handle_heartbeat(1, bytes([0x00]))
        assert state == "BOOT_UP"

    def test_operational_state(self):
        nmt = NMTMonitor()
        state = nmt.handle_heartbeat(1, bytes([0x05]))
        assert state == "OPERATIONAL"

    def test_pre_operational_state(self):
        nmt = NMTMonitor()
        state = nmt.handle_heartbeat(1, bytes([0x7F]))
        assert state == "PRE_OPERATIONAL"

    def test_stopped_state(self):
        nmt = NMTMonitor()
        state = nmt.handle_heartbeat(1, bytes([0x04]))
        assert state == "STOPPED"

    def test_state_transition_detected(self):
        nmt = NMTMonitor()
        nmt.handle_heartbeat(1, bytes([0x00]))
        state = nmt.handle_heartbeat(1, bytes([0x7F]))
        assert state == "PRE_OPERATIONAL"

    def test_no_change_returns_none(self):
        nmt = NMTMonitor()
        nmt.handle_heartbeat(1, bytes([0x05]))
        state = nmt.handle_heartbeat(1, bytes([0x05]))
        assert state is None

    def test_multiple_nodes(self):
        nmt = NMTMonitor()
        nmt.handle_heartbeat(1, bytes([0x05]))
        nmt.handle_heartbeat(2, bytes([0x7F]))

        states = nmt.get_node_states()
        assert states[1] == "OPERATIONAL"
        assert states[2] == "PRE_OPERATIONAL"

    def test_nmt_command(self):
        nmt = NMTMonitor()
        result = nmt.handle_nmt_command(bytes([0x01, 0x00]))
        assert result == (1, 0)

    def test_empty_data_heartbeat(self):
        nmt = NMTMonitor()
        state = nmt.handle_heartbeat(1, b"")
        assert state is None


class TestEmergencyDecoding:
    """Test EMERGENCY message decoding."""

    def test_decode_emergency(self):
        data = bytes([0x10, 0x42, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00])
        emcy = decode_emergency(1, data)
        assert emcy is not None
        assert emcy.node_id == 1
        assert emcy.error_code == 0x4210
        assert emcy.error_register == 0x04

    def test_short_data_returns_none(self):
        emcy = decode_emergency(1, bytes(2))
        assert emcy is None
