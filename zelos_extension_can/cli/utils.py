"""Common CLI utilities."""

import logging
import signal
import sys
from types import FrameType

logger = logging.getLogger(__name__)


def setup_shutdown_handler() -> None:
    """Exit on SIGINT/SIGTERM; each entry point stops its codecs in a finally."""

    def shutdown_handler(signum: int, frame: FrameType | None) -> None:
        """Handle graceful shutdown.

        :param signum: Signal number
        :param frame: Current stack frame
        """
        logger.info("Shutting down CAN extension...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
