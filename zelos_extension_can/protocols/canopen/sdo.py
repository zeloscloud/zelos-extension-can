"""CANopen SDO (Service Data Object) passive observer.

Passively correlates SDO request/response pairs for monitoring.
Supports expedited and segmented transfers per CiA 301.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# SDO command specifiers (upper 3 bits of first byte)
SDO_CCS_DOWNLOAD_INIT = 1  # Client -> Server: Initiate download
SDO_CCS_UPLOAD_INIT = 2  # Client -> Server: Initiate upload
SDO_SCS_UPLOAD_INIT = 2  # Server -> Client: Initiate upload response
SDO_SCS_DOWNLOAD_INIT = 3  # Server -> Client: Initiate download response
SDO_ABORT = 4  # Abort transfer


@dataclass(slots=True)
class SDOTransfer:
    """Completed SDO transfer record."""

    node_id: int
    index: int
    subindex: int
    direction: str  # "upload" (read) or "download" (write)
    data: bytes | None
    timestamp: float
    expedited: bool


class SDOObserver:
    """Passive observer for SDO transfers."""

    def __init__(self, max_history: int = 100, pending_timeout_s: float = 30.0) -> None:
        self._pending: dict[tuple[int, int, int], dict] = {}  # (node_id, index, subindex) -> info
        self._history: list[SDOTransfer] = []
        self._max_history = max_history
        self._pending_timeout_s = pending_timeout_s
        self.total_transfers = 0
        self.aborted_transfers = 0

    def handle(
        self,
        node_id: int,
        is_response: bool,
        data: bytes,
    ) -> SDOTransfer | None:
        """Process an SDO frame (request or response).

        :param node_id: Node ID
        :param is_response: True if SDO_TX (server response), False if SDO_RX (client request)
        :param data: SDO frame data (8 bytes)
        :return: Completed SDOTransfer if a transfer finished, else None
        """
        if len(data) < 4:
            return None

        cmd = data[0]
        cs = (cmd >> 5) & 0x07  # Command specifier

        if cs == SDO_ABORT:
            index = data[1] | (data[2] << 8)
            subindex = data[3]
            key = (node_id, index, subindex)
            self._pending.pop(key, None)
            self.aborted_transfers += 1
            return None

        index = data[1] | (data[2] << 8)
        subindex = data[3]
        key = (node_id, index, subindex)

        if not is_response:
            # Client request (SDO_RX)
            if cs == SDO_CCS_UPLOAD_INIT:
                # Initiate upload (read) request
                self._pending[key] = {
                    "direction": "upload",
                    "timestamp": time.monotonic(),
                }
            elif cs == SDO_CCS_DOWNLOAD_INIT:
                # Initiate download (write) request
                expedited = bool(cmd & 0x02)
                if expedited:
                    # Expedited download — data is in this frame
                    n = (cmd >> 2) & 0x03 if (cmd & 0x01) else 0
                    sdo_data = bytes(data[4 : 8 - n]) if n else bytes(data[4:8])
                    self._pending[key] = {
                        "direction": "download",
                        "data": sdo_data,
                        "expedited": True,
                        "timestamp": time.monotonic(),
                    }
                else:
                    self._pending[key] = {
                        "direction": "download",
                        "expedited": False,
                        "timestamp": time.monotonic(),
                    }
        else:
            # Server response (SDO_TX)
            pending = self._pending.pop(key, None)
            if pending is None:
                return None

            if pending["direction"] == "upload" and cs == SDO_SCS_UPLOAD_INIT:
                # Upload response
                expedited = bool(cmd & 0x02)
                if expedited:
                    n = (cmd >> 2) & 0x03 if (cmd & 0x01) else 0
                    sdo_data = bytes(data[4 : 8 - n]) if n else bytes(data[4:8])
                else:
                    sdo_data = None  # Segmented — not fully tracked

                transfer = SDOTransfer(
                    node_id=node_id,
                    index=index,
                    subindex=subindex,
                    direction="upload",
                    data=sdo_data,
                    timestamp=time.monotonic(),
                    expedited=expedited,
                )
                self._record_transfer(transfer)
                return transfer

            elif pending["direction"] == "download" and cs == SDO_SCS_DOWNLOAD_INIT:
                # Download confirmed
                transfer = SDOTransfer(
                    node_id=node_id,
                    index=index,
                    subindex=subindex,
                    direction="download",
                    data=pending.get("data"),
                    timestamp=time.monotonic(),
                    expedited=pending.get("expedited", False),
                )
                self._record_transfer(transfer)
                return transfer

        return None

    def cleanup_stale(self) -> int:
        """Remove stale pending SDO requests that never got a response.

        :return: Number of stale entries removed
        """
        now = time.monotonic()
        stale_keys = [
            key
            for key, info in self._pending.items()
            if (now - info["timestamp"]) > self._pending_timeout_s
        ]
        for key in stale_keys:
            self._pending.pop(key)
        return len(stale_keys)

    def _record_transfer(self, transfer: SDOTransfer) -> None:
        self._history.append(transfer)
        self.total_transfers += 1
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

    def get_recent_transfers(self, count: int = 20) -> list[dict[str, Any]]:
        """Get recent SDO transfers."""
        entries = []
        for t in self._history[-count:]:
            entries.append(
                {
                    "node_id": t.node_id,
                    "index": f"0x{t.index:04X}",
                    "subindex": t.subindex,
                    "direction": t.direction,
                    "expedited": t.expedited,
                    "data_hex": t.data.hex() if t.data else None,
                }
            )
        return entries
