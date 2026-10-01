"""PEAK PCAN driver helpers."""

import logging

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
