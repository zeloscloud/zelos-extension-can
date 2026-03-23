"""CANopen protocol utilities: COB-ID parsing, NMT state tracking, EMERGENCY decoding.

Implements CiA 301 base protocol for passive monitoring of CANopen networks.
"""

import logging
from dataclasses import dataclass
from enum import IntEnum

logger = logging.getLogger(__name__)


class FunctionCode(IntEnum):
    """CANopen function codes (upper bits of 11-bit COB-ID)."""

    NMT = 0x000
    SYNC = 0x080
    EMERGENCY = 0x080  # + node_id (same base as SYNC, distinguished by node_id > 0)
    TPDO1 = 0x180
    RPDO1 = 0x200
    TPDO2 = 0x280
    RPDO2 = 0x300
    TPDO3 = 0x380
    RPDO3 = 0x400
    TPDO4 = 0x480
    RPDO4 = 0x500
    SDO_TX = 0x580
    SDO_RX = 0x600
    HEARTBEAT = 0x700


class NMTState(IntEnum):
    """CANopen NMT states."""

    BOOT_UP = 0x00
    STOPPED = 0x04
    OPERATIONAL = 0x05
    PRE_OPERATIONAL = 0x7F


PDO_FUNCTION_CODES = frozenset(
    {
        FunctionCode.TPDO1,
        FunctionCode.RPDO1,
        FunctionCode.TPDO2,
        FunctionCode.RPDO2,
        FunctionCode.TPDO3,
        FunctionCode.RPDO3,
        FunctionCode.TPDO4,
        FunctionCode.RPDO4,
    }
)


def parse_cob_id(arb_id: int) -> tuple[FunctionCode | None, int]:
    """Parse an 11-bit CANopen COB-ID into function code and node ID."""
    if arb_id == 0x000:
        return (FunctionCode.NMT, 0)
    if arb_id == 0x080:
        return (FunctionCode.SYNC, 0)
    if 0x701 <= arb_id <= 0x77F:
        return (FunctionCode.HEARTBEAT, arb_id - 0x700)
    if 0x581 <= arb_id <= 0x5FF:
        return (FunctionCode.SDO_TX, arb_id - 0x580)
    if 0x601 <= arb_id <= 0x67F:
        return (FunctionCode.SDO_RX, arb_id - 0x600)
    if 0x181 <= arb_id <= 0x1FF:
        return (FunctionCode.TPDO1, arb_id - 0x180)
    if 0x201 <= arb_id <= 0x27F:
        return (FunctionCode.RPDO1, arb_id - 0x200)
    if 0x281 <= arb_id <= 0x2FF:
        return (FunctionCode.TPDO2, arb_id - 0x280)
    if 0x301 <= arb_id <= 0x37F:
        return (FunctionCode.RPDO2, arb_id - 0x300)
    if 0x381 <= arb_id <= 0x3FF:
        return (FunctionCode.TPDO3, arb_id - 0x380)
    if 0x401 <= arb_id <= 0x47F:
        return (FunctionCode.RPDO3, arb_id - 0x400)
    if 0x481 <= arb_id <= 0x4FF:
        return (FunctionCode.TPDO4, arb_id - 0x480)
    if 0x501 <= arb_id <= 0x57F:
        return (FunctionCode.RPDO4, arb_id - 0x500)
    if 0x081 <= arb_id <= 0x0FF:
        return (FunctionCode.EMERGENCY, arb_id - 0x080)
    return (None, 0)


@dataclass(slots=True)
class EmergencyMessage:
    """Decoded CANopen EMERGENCY message."""

    node_id: int
    error_code: int
    error_register: int


class NMTMonitor:
    """Tracks NMT states for all nodes on the network."""

    def __init__(self) -> None:
        self._node_states: dict[int, NMTState] = {}

    def handle_heartbeat(self, node_id: int, data: bytes) -> str | None:
        """Process a heartbeat message and update node state.

        :return: State name string if state changed, None otherwise
        """
        if len(data) < 1:
            return None

        state_byte = data[0] & 0x7F
        try:
            new_state = NMTState(state_byte)
        except ValueError:
            return None

        old_state = self._node_states.get(node_id)
        self._node_states[node_id] = new_state

        if old_state != new_state:
            return new_state.name
        return None

    def handle_nmt_command(self, data: bytes) -> tuple[int, int] | None:
        """Process an NMT command message.

        :return: (command, target_node) or None
        """
        if len(data) < 2:
            return None
        return (data[0], data[1])

    def get_node_states(self) -> dict[int, str]:
        return {node_id: state.name for node_id, state in sorted(self._node_states.items())}

    def get_node_count(self) -> int:
        return len(self._node_states)


def decode_emergency(node_id: int, data: bytes) -> EmergencyMessage | None:
    """Decode a CANopen EMERGENCY message."""
    if len(data) < 3:
        return None

    return EmergencyMessage(
        node_id=node_id,
        error_code=data[0] | (data[1] << 8),
        error_register=data[2],
    )
