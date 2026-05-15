"""CANopen PDO (Process Data Object) decoder.

Decodes PDO frames using EDS-derived mappings when available,
or emits raw PDO data for unmapped PDOs.
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PDOMapping:
    """Single PDO mapping entry."""

    name: str
    index: int
    subindex: int
    bit_length: int
    signed: bool = False


class PDODecoder:
    """Decodes PDO frames using EDS-based mappings."""

    def __init__(self) -> None:
        # Maps COB-ID -> list of PDOMapping entries
        self._mappings: dict[int, list[PDOMapping]] = {}

    def add_mapping(self, cob_id: int, mappings: list[PDOMapping]) -> None:
        """Add PDO mappings for a COB-ID.

        :param cob_id: PDO COB-ID
        :param mappings: Ordered list of PDO mapping entries
        """
        self._mappings[cob_id] = mappings
        logger.debug("Added PDO mapping for COB-ID 0x%03X: %d signals", cob_id, len(mappings))

    @property
    def mapping_count(self) -> int:
        return len(self._mappings)

    def has_mapping(self, cob_id: int) -> bool:
        return cob_id in self._mappings

    def get_all_mappings(self) -> dict[int, list[PDOMapping]]:
        """Get all configured mappings (COB-ID -> mapping list)."""
        return self._mappings

    def decode(self, cob_id: int, data: bytes) -> dict[str, Any] | None:
        """Decode a PDO frame using stored mappings.

        :param cob_id: PDO COB-ID
        :param data: PDO frame data
        :return: Dict of signal_name -> value, or None if no mapping
        """
        mappings = self._mappings.get(cob_id)
        if not mappings:
            return None

        result = {}
        bit_offset = 0

        for mapping in mappings:
            if bit_offset + mapping.bit_length > len(data) * 8:
                break

            value = self._extract_bits(data, bit_offset, mapping.bit_length, mapping.signed)
            result[mapping.name] = value
            bit_offset += mapping.bit_length

        return result

    @staticmethod
    def _extract_bits(data: bytes, bit_offset: int, bit_length: int, signed: bool) -> int | float:
        """Extract a value from data at the given bit offset and length.

        :param data: Raw bytes
        :param bit_offset: Starting bit position
        :param bit_length: Number of bits
        :param signed: Whether the value is signed
        :return: Extracted value
        """
        # Convert to integer for bit manipulation
        byte_start = bit_offset // 8
        bit_start_in_byte = bit_offset % 8

        # Read enough bytes
        bytes_needed = (bit_start_in_byte + bit_length + 7) // 8
        if byte_start + bytes_needed > len(data):
            return 0

        # Build value from bytes (little-endian)
        raw_value = 0
        for i in range(bytes_needed):
            raw_value |= data[byte_start + i] << (i * 8)

        # Shift and mask
        raw_value >>= bit_start_in_byte
        mask = (1 << bit_length) - 1
        raw_value &= mask

        # Handle signed values
        if signed and bit_length > 1 and raw_value & (1 << (bit_length - 1)):
            raw_value -= 1 << bit_length

        return raw_value
