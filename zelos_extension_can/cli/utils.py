"""Common CLI utilities."""

import logging
import signal
import sys
from types import FrameType
from typing import Any

import rich_click as click
from zelos_can.params import parse_canopen_node_spec

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


def canopen_node_option(fn: Any) -> Any:
    """`--canopen` and the repeatable `--canopen-node ID[:FILE[:NAME]]`."""

    def parse(_ctx: Any, _param: Any, specs: tuple[str, ...]) -> list[dict[str, Any]]:
        try:
            return [parse_canopen_node_spec(spec) for spec in specs]
        except (ValueError, FileNotFoundError) as e:
            raise click.BadParameter(str(e)) from None

    fn = click.option(
        "--canopen-node",
        "canopen_nodes",
        multiple=True,
        metavar="ID[:FILE[:NAME]]",
        callback=parse,
        help="A CANopen node: id (5 or 0x05), its EDS/DCF, a name for its events. "
        "Repeatable; FILE may be empty (5::left)",
    )(fn)
    return click.option(
        "--canopen", is_flag=True, help="Decode CANopen for every node-id, with no --canopen-node"
    )(fn)
