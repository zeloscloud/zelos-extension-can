"""Base protocol handler abstraction for CAN bus protocols."""

import abc
import logging
from typing import Any

import can
import zelos_sdk

logger = logging.getLogger(__name__)


class ProtocolHandler(abc.ABC):
    """Abstract base class for CAN protocol handlers (J1939, CANopen, etc.).

    Protocol handlers intercept CAN frames before DBC decoding and can:
    - Consume frames entirely (return True from handle_frame)
    - Add metadata alongside DBC decoding (return False from handle_frame)
    - Perform protocol-specific reassembly (e.g., J1939 transport protocol)
    """

    def __init__(
        self,
        config: dict,
        source: zelos_sdk.TraceSource,
        namespace: zelos_sdk.TraceNamespace | None,
        bus_name: str | None,
    ) -> None:
        self.config = config
        self.source = source
        self.namespace = namespace
        self.bus_name = bus_name
        self._codec = None

    def set_codec(self, codec: Any) -> None:
        """Set reference to parent codec for protocol-specific decode (e.g., TP reassembly).

        :param codec: CanCodec instance
        """
        self._codec = codec

    def _create_trace_source(self, name: str) -> zelos_sdk.TraceSource:
        """Create a trace source with optional namespace.

        :param name: Source name
        :return: TraceSource instance
        """
        if self.namespace:
            return zelos_sdk.TraceSource(name, namespace=self.namespace)
        return zelos_sdk.TraceSource(name)

    def _log_event(self, event: Any, timestamp_ns: int | None, **kwargs: Any) -> None:
        """Emit a trace event with timestamp handling and error suppression.

        :param event: Trace event to emit
        :param timestamp_ns: Timestamp in nanoseconds, or None
        :param kwargs: Event field values
        """
        try:
            if timestamp_ns is not None:
                event.log_at(timestamp_ns, **kwargs)
            else:
                event.log(**kwargs)
        except (OverflowError, TypeError, ValueError) as e:
            logger.debug("Error emitting trace event: %s", e)

    @abc.abstractmethod
    def handle_frame(self, msg: can.Message, timestamp_ns: int | None) -> bool:
        """Process a CAN frame.

        :param msg: CAN message
        :param timestamp_ns: Timestamp in nanoseconds, or None
        :return: True if consumed (skip DBC decode), False to continue to DBC path
        """

    @abc.abstractmethod
    def get_status(self) -> dict[str, Any]:
        """Get protocol handler status information."""

    @abc.abstractmethod
    def get_metrics(self) -> dict[str, Any]:
        """Get protocol handler metrics."""

    def cleanup(self) -> None:  # noqa: B027
        """Called periodically from health loop. Clean stale state."""
