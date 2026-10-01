"""PEAK PCAN driver helpers."""

import logging
from typing import Any

import can

logger = logging.getLogger(__name__)


def release_pcan_channel(channel: str) -> None:
    """Release a PCAN channel a failed open left initialized.

    For a few seconds after its previous user is killed, the driver keeps the
    channel at that user's bitrate, and opening it at another one returns
    PCAN_ERROR_CAUTION. python-can raises on it but leaves the channel
    initialized, so every retry fails as "not initialized" until it is released.
    """
    from can.interfaces.pcan.basic import PCAN_CHANNEL_NAMES, PCANBasic

    handle = PCAN_CHANNEL_NAMES.get(channel)
    if handle is None:
        return
    try:
        PCANBasic().Uninitialize(handle)
    except Exception:  # no PCAN library: nothing was initialized to release
        logger.debug("Could not release PCAN channel %s", channel, exc_info=True)


def pcan_controller_state(bus: Any) -> str | None:
    """A PCAN controller's error state, or None for any other bus.

    python-can's `bus.state` is the mode the bus was opened in, never the
    controller's error state, so PCAN's own status is read instead."""
    try:
        from can.interfaces.pcan import PcanBus
        from can.interfaces.pcan import basic as pcan
    except Exception:
        return None
    if not isinstance(bus, PcanBus):
        return None
    try:
        code = bus.status()
    except Exception:
        return "unavailable"
    # Handle codes share bits, so the handle field is compared, not masked.
    handle = code & pcan.PCAN_ERROR_ILLHANDLE
    gone = pcan.PCAN_ERROR_NODRIVER | pcan.PCAN_ERROR_REGTEST | pcan.PCAN_ERROR_INITIALIZE
    if code & gone or handle in (
        pcan.PCAN_ERROR_ILLHW,
        pcan.PCAN_ERROR_ILLNET,
        pcan.PCAN_ERROR_ILLCLIENT,
    ):
        return "unavailable"
    if code & pcan.PCAN_ERROR_BUSOFF:
        return "bus_off"
    if code & pcan.PCAN_ERROR_BUSPASSIVE:
        return "passive"
    if code & (pcan.PCAN_ERROR_BUSHEAVY | pcan.PCAN_ERROR_BUSLIGHT):
        return "warning"
    return "ok"


# A controller clock every PEAK CAN FD adapter supports.
PCAN_FD_CLOCK_HZ = 80_000_000
# python-can's PcanBus default when no bitrate is given.
PCAN_DEFAULT_BITRATE = 500_000
# Advanced options that already give PCAN its timing.
PCAN_TIMING_KEYS = {"timing", "f_clock", "f_clock_mhz", "nom_brp", "nom_tseg1"}


def pcan_fd_timing(nominal: int, data: int) -> can.BitTimingFd:
    """CAN FD bit timing for a PCAN adapter, sampling at 80% in both phases."""
    if data < nominal:
        raise ValueError(
            f"CAN FD Data Bitrate ({data}) is below Bitrate ({nominal}); set it at or above."
        )
    return can.BitTimingFd.from_sample_point(
        f_clock=PCAN_FD_CLOCK_HZ,
        nom_bitrate=nominal,
        nom_sample_point=80.0,
        data_bitrate=data,
        data_sample_point=80.0,
    )
