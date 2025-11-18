"""Common CLI utilities."""

import logging
import signal
import sys
from types import FrameType

logger = logging.getLogger(__name__)


def setup_shutdown_handler(codec) -> None:
    """Setup signal handlers for graceful shutdown.

    :param codec: CAN codec instance to stop on shutdown
    """

    def shutdown_handler(signum: int, frame: FrameType | None) -> None:
        """Handle graceful shutdown.

        :param signum: Signal number
        :param frame: Current stack frame
        """
        logger.info("Shutting down CAN extension...")
        codec.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
