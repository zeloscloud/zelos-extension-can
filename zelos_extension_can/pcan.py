"""PEAK PCAN driver helpers."""

import logging

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
