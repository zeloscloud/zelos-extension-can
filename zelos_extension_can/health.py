"""Bus health: the controller's error state as the adapter reports it,
refined by what the bus saw lately, and the `status` get_tx_state publishes."""

import time
from typing import Any

import can

# How long a failed send or an error frame keeps its mark on the bus's health.
RECENT_S = 5.0

CHECK_THE_WIRE = "Check the wiring, termination, and that another node runs at this bitrate."
HEALTH_DETAIL = {
    "warning": (
        "The controller's error counters are high: frames have been failing on the wire. "
        + CHECK_THE_WIRE
    ),
    "passive": f"The controller is error-passive: most frames are failing. {CHECK_THE_WIRE}",
    "bus_off": "The controller is bus-off and sends nothing until the bus restarts.",
    "unavailable": "The adapter is not responding. Check its USB connection.",
}
RECONNECTING = "The bus is down; reconnecting to the adapter."
NO_ACK = f"No node acknowledged the frames sent. {CHECK_THE_WIRE}"
WIRE_ERRORS = f"The controller saw errors on the wire. {CHECK_THE_WIRE}"
OVERFLOW = "The controller's buffer overflowed: frames were lost."

# linux/can/error.h: the error class in an error frame's id, and the controller
# state in data[1] under CAN_ERR_CRTL.
_CAN_ERR_TX_TIMEOUT = 0x01
_CAN_ERR_CRTL = 0x04
_CAN_ERR_PROT = 0x08
_CAN_ERR_TRX = 0x10
_CAN_ERR_ACK = 0x20
_CAN_ERR_BUSOFF = 0x40
_CAN_ERR_BUSERROR = 0x80
_CAN_ERR_RESTARTED = 0x100
_CAN_ERR_CRTL_OVERFLOW = 0x01 | 0x02
_CAN_ERR_CRTL_WARNING = 0x04 | 0x08
_CAN_ERR_CRTL_PASSIVE = 0x10 | 0x20
_CAN_ERR_CRTL_ACTIVE = 0x40


def socketcan_error_frame(msg: can.Message) -> tuple[str, str | None] | None:
    """The controller state and reason one SocketCAN error frame reports, or
    None for one that says nothing of health, such as lost arbitration."""
    error_class = msg.arbitration_id
    controller = msg.data[1] if error_class & _CAN_ERR_CRTL and len(msg.data) > 1 else 0
    no_ack = error_class & _CAN_ERR_ACK
    if error_class & _CAN_ERR_BUSOFF:
        return "bus_off", HEALTH_DETAIL["bus_off"]
    if controller & _CAN_ERR_CRTL_PASSIVE:
        return "passive", NO_ACK if no_ack else HEALTH_DETAIL["passive"]
    if controller & _CAN_ERR_CRTL_WARNING:
        return "warning", NO_ACK if no_ack else HEALTH_DETAIL["warning"]
    if no_ack:
        return "warning", NO_ACK
    if error_class & (_CAN_ERR_PROT | _CAN_ERR_BUSERROR | _CAN_ERR_TRX | _CAN_ERR_TX_TIMEOUT):
        return "warning", WIRE_ERRORS
    if controller & _CAN_ERR_CRTL_OVERFLOW:
        return "warning", OVERFLOW
    if error_class & _CAN_ERR_RESTARTED or controller & _CAN_ERR_CRTL_ACTIVE:
        return "ok", None
    return None


def derive_bus_status(running: bool, bus: Any) -> str:
    """Map (running, python-can BusState) to stopped / active / error / unknown.
    `BusHealth.report` refines it with the controller's own error state.

    Virtual / fake / file backends often raise on `bus.state` or don't
    return a real `can.BusState` enum; on those we trust `running` and
    fall back to "active"."""
    if not running or bus is None:
        return "stopped"
    try:
        state = bus.state
    except Exception:
        return "active"
    if not isinstance(state, can.BusState):
        return "active"
    if state == can.BusState.ACTIVE:
        return "active"
    if state in {can.BusState.ERROR, can.BusState.PASSIVE}:
        return "error"
    return "unknown"


class BusHealth:
    """What a bus's recent failed sends and error frames add to the state
    its adapter reports."""

    def __init__(self, interface: str | None) -> None:
        self._interface = interface
        # The latest of each, with the monotonic time it happened.
        self._tx_failure: tuple[float, str] | None = None
        self._error_frame: tuple[float, str, str | None] | None = None

    def note_tx_failure(self, reason: str) -> None:
        self._tx_failure = (time.monotonic(), reason)

    def note_error_frame(self, msg: can.Message) -> None:
        """Let an error frame mark the bus.

        SocketCAN's error frames carry linux/can/error.h's class and the
        controller's state; other adapters' say only that the bus saw errors."""
        if self._interface == "socketcan-py":
            reported = socketcan_error_frame(msg)
        else:
            reported = ("warning", WIRE_ERRORS)
        if reported is not None:
            self._error_frame = (time.monotonic(), *reported)

    def report(
        self,
        running: bool,
        bus: Any,
        state: str | None,
        detail: str | None,
        counters: dict[str, int],
    ) -> tuple[str, dict[str, Any]]:
        """`status` and `health` for get_tx_state, from the error state the
        adapter reports (None when it reports none), why, and its counters."""
        if not running:
            return "stopped", {"state": "unknown", "detail": None}
        if bus is None:
            return "error", {"state": "unavailable", "detail": RECONNECTING}
        # An error frame is up to RECENT_S old: it names the state only for an
        # adapter that reports none, and otherwise refines the reason.
        if self._recent(self._error_frame):
            _, frame_state, frame_detail = self._error_frame
            if state is None:
                state, detail = frame_state, frame_detail
            elif frame_state == state and frame_detail:
                detail = frame_detail
        # What went wrong comes first, then the controller's reason for it.
        happened = []
        if self._recent(self._tx_failure):
            happened.append(f"Frames could not be sent: {self._tx_failure[1]}.")
        if happened:
            detail = " ".join([*happened, *([detail] if detail else [])])
        health = {"state": state or "unknown", "detail": detail, **counters}
        return self._status(bus, health["state"]), health

    def _status(self, bus: Any, state: str) -> str:
        """active / warning / error / unknown: the controller's error state,
        worsened by sends that fail, or else the transport's verdict."""
        if state in ("passive", "bus_off", "unavailable") or self._recent(self._tx_failure):
            return "error"
        status = derive_bus_status(True, bus)
        if status != "error" and state == "warning":
            return "warning"
        return status

    @staticmethod
    def _recent(event: tuple[Any, ...] | None) -> bool:
        return event is not None and time.monotonic() - event[0] < RECENT_S
