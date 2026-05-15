"""CANopen EDS (Electronic Data Sheet) file loading and PDO mapping extraction."""

import logging
from pathlib import Path

from .pdo import PDODecoder, PDOMapping

logger = logging.getLogger(__name__)


def build_pdo_decoder_from_eds(eds_path: str, node_id: int) -> PDODecoder:
    """Build a PDO decoder from an EDS file for a specific node.

    Extracts TPDO mapping parameters (0x1A00-0x1A03) and creates
    PDOMapping entries for each mapped object.
    """
    import canopen

    path = Path(eds_path)
    if not path.exists():
        raise FileNotFoundError(f"EDS file not found: {eds_path}")

    od = canopen.ObjectDictionary()
    od.load(str(path))
    decoder = PDODecoder()

    tpdo_bases = [
        (0x1800, 0x1A00, 0x180),
        (0x1801, 0x1A01, 0x280),
        (0x1802, 0x1A02, 0x380),
        (0x1803, 0x1A03, 0x480),
    ]

    for comm_idx, mapping_idx, default_cob_base in tpdo_bases:
        try:
            if comm_idx in od:
                comm_obj = od[comm_idx]
                if hasattr(comm_obj, "__getitem__") and 1 in comm_obj:
                    cob_id = comm_obj[1].default
                    if isinstance(cob_id, int):
                        if cob_id & 0x80000000:
                            continue
                        cob_id &= 0x7FF
                    else:
                        cob_id = default_cob_base + node_id
                else:
                    cob_id = default_cob_base + node_id
            else:
                cob_id = default_cob_base + node_id

            if mapping_idx not in od:
                continue

            mapping_obj = od[mapping_idx]
            mappings = []

            if hasattr(mapping_obj, "__getitem__") and 0 in mapping_obj:
                num_mapped = mapping_obj[0].default
                if not isinstance(num_mapped, int) or num_mapped == 0:
                    continue
            else:
                continue

            for sub_i in range(1, num_mapped + 1):
                if sub_i not in mapping_obj:
                    continue

                mapping_value = mapping_obj[sub_i].default
                if not isinstance(mapping_value, int) or mapping_value == 0:
                    continue

                obj_index = (mapping_value >> 16) & 0xFFFF
                obj_subindex = (mapping_value >> 8) & 0xFF
                bit_length = mapping_value & 0xFF

                name = f"0x{obj_index:04X}_{obj_subindex}"
                if obj_index in od:
                    obj = od[obj_index]
                    if hasattr(obj, "name"):
                        name = obj.name
                    if hasattr(obj, "__getitem__") and obj_subindex in obj:
                        sub_obj = obj[obj_subindex]
                        if hasattr(sub_obj, "name"):
                            name = sub_obj.name

                mappings.append(
                    PDOMapping(
                        name=name,
                        index=obj_index,
                        subindex=obj_subindex,
                        bit_length=bit_length,
                    )
                )

            if mappings:
                decoder.add_mapping(cob_id, mappings)

        except Exception as e:
            logger.debug("Error parsing TPDO at 0x%04X: %s", mapping_idx, e)
            continue

    return decoder
