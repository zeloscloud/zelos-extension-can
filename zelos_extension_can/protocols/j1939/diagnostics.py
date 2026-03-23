"""J1939 DM1/DM2 diagnostic trouble code decoder.

Decodes Active Diagnostic Trouble Codes (DM1, PGN 65226) and
Previously Active DTCs (DM2, PGN 65227) per SAE J1939-73.
"""

from dataclasses import dataclass


@dataclass(slots=True)
class DTC:
    """J1939 Diagnostic Trouble Code."""

    spn: int  # Suspect Parameter Number (19 bits)
    fmi: int  # Failure Mode Identifier (5 bits)
    occurrence: int  # Occurrence count (7 bits)


def decode_dm1(data: bytes) -> tuple[int, list[DTC]]:
    """Decode DM1/DM2 payload into lamp status and DTCs.

    DM1 format:
    - Byte 0-1: Lamp status (protect, amber warning, red stop, malfunction)
    - Bytes 2+: DTCs, 4 bytes each:
      - Byte 0-1: SPN bits [7:0] and [15:8]
      - Byte 2[7:5]: SPN bits [18:16], [4:0]: FMI
      - Byte 3[6:0]: Occurrence count, [7]: SPN conversion method

    :param data: DM1/DM2 payload bytes (may be from TP reassembly)
    :return: Tuple of (lamp_status_byte, list of DTCs)
    """
    if len(data) < 2:
        return (0, [])

    lamp_status = data[0] | (data[1] << 8)
    dtcs: list[DTC] = []

    # DTCs start at byte 2, each is 4 bytes
    offset = 2
    while offset + 3 < len(data):
        byte0 = data[offset]
        byte1 = data[offset + 1]
        byte2 = data[offset + 2]
        byte3 = data[offset + 3]

        # SPN: 19 bits across bytes 0, 1, and upper 3 bits of byte 2
        spn_low = byte0 | (byte1 << 8)
        spn_high = (byte2 >> 5) & 0x07
        spn = spn_low | (spn_high << 16)

        # FMI: lower 5 bits of byte 2
        fmi = byte2 & 0x1F

        # Occurrence count: lower 7 bits of byte 3
        occurrence = byte3 & 0x7F

        # Skip "no fault" indicator (all 1s)
        if spn == 0x7FFFF and fmi == 0x1F:
            offset += 4
            continue

        dtcs.append(DTC(spn=spn, fmi=fmi, occurrence=occurrence))
        offset += 4

    return (lamp_status, dtcs)
