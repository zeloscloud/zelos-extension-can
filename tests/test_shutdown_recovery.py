"""Tests for shutdown ordering and bus recovery."""

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import can
import pytest

from zelos_extension_can.codec import CanCodec


@pytest.fixture
def test_dbc_path():
    return str(Path(__file__).parent / "files" / "test.dbc")


@pytest.fixture
def codec(test_dbc_path):
    config = {
        "interface": "virtual",
        "channel": "vcan0",
        "database_file": test_dbc_path,
    }
    with patch("zelos_sdk.TraceSource"):
        return CanCodec(config)


class TestShutdownOrdering:
    """Test that shutdown stops notifier before closing bus socket."""

    def test_stop_sets_running_false(self, codec):
        """stop() should signal but not close the bus directly."""
        codec.running = True
        codec.bus = MagicMock()
        codec.stop()

        assert codec.running is False
        # Bus should NOT be shut down by stop() — _run_async's finally does that
        codec.bus.shutdown.assert_not_called()

    def test_stop_cancels_demo_task(self, codec):
        """stop() should cancel the demo task."""
        mock_task = MagicMock()
        codec.demo_task = mock_task
        codec.stop()

        mock_task.cancel.assert_called_once()
        assert codec.demo_task is None

    def test_stop_cancels_periodic_tasks(self, codec):
        """stop() should cancel all periodic tasks."""
        task1 = MagicMock()
        task2 = MagicMock()
        codec.periodic_tasks = {"t1": task1, "t2": task2}
        codec.stop()

        task1.cancel.assert_called_once()
        task2.cancel.assert_called_once()
        assert len(codec.periodic_tasks) == 0

    def test_run_async_finally_stops_notifier_then_bus(self, codec):
        """_run_async's finally block should stop notifier before bus."""
        shutdown_order = []

        mock_bus = MagicMock()
        mock_bus.shutdown.side_effect = lambda: shutdown_order.append("bus.shutdown")
        mock_bus.state = can.BusState.ACTIVE
        codec.bus = mock_bus

        mock_notifier = MagicMock()
        mock_notifier.stop.side_effect = lambda: shutdown_order.append("notifier.stop")

        with patch("can.Notifier", return_value=mock_notifier):

            async def run_and_cancel():
                task = asyncio.create_task(codec._run_async())
                await asyncio.sleep(0.1)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            asyncio.run(run_and_cancel())

        assert shutdown_order == ["notifier.stop", "bus.shutdown"]
        assert codec.bus is None


class TestBusRecovery:
    """Test bus health monitoring and reconnection."""

    def test_check_bus_health_virtual(self, codec):
        """Virtual bus is always considered healthy if bus object exists."""
        codec.bus = MagicMock()
        assert codec._check_bus_health() is True

    def test_check_bus_health_no_bus(self, codec):
        """No bus object means unhealthy."""
        codec.bus = None
        assert codec._check_bus_health() is False

    def test_check_bus_health_hardware_active(self, codec):
        """Hardware bus in ACTIVE state is healthy."""
        codec.config["interface"] = "socketcan"
        codec.bus = MagicMock()
        codec.bus.state = can.BusState.ACTIVE
        assert codec._check_bus_health() is True

    def test_check_bus_health_hardware_error(self, codec):
        """Hardware bus in ERROR state is unhealthy."""
        codec.config["interface"] = "socketcan"
        codec.bus = MagicMock()
        codec.bus.state = can.BusState.ERROR
        assert codec._check_bus_health() is False

    def test_reconnect_bus(self, codec):
        """Reconnection shuts down old bus and starts new one."""
        old_bus = MagicMock()
        codec.bus = old_bus

        with patch.object(codec, "start"):
            result = asyncio.run(codec._reconnect_bus())

        assert result is True
        old_bus.shutdown.assert_called_once()

    def test_reconnect_bus_failure(self, codec):
        """Failed reconnection returns False."""
        codec.bus = MagicMock()

        with patch.object(codec, "start", side_effect=Exception("connection refused")):
            result = asyncio.run(codec._reconnect_bus())

        assert result is False

    def test_handle_reconnection_stops_notifier_first(self, codec):
        """Reconnection stops the old notifier before creating a new one."""
        old_notifier = MagicMock()
        codec.bus = MagicMock()

        with (
            patch.object(codec, "_reconnect_bus", return_value=True),
            patch("can.Notifier"),
        ):
            asyncio.run(codec._handle_reconnection(old_notifier))

        old_notifier.stop.assert_called_once()

    def test_message_handling_after_stop_is_safe(self, codec):
        """Messages arriving after stop() should not crash."""
        codec.running = False
        msg = can.Message(
            arbitration_id=0x64,
            data=bytes(8),
            timestamp=15.5,
        )
        # Should not raise
        codec._handle_message(msg)


class TestMultipleStopCalls:
    """Test that calling stop() multiple times is safe."""

    def test_double_stop(self, codec):
        """Calling stop() twice should not raise."""
        codec.bus = MagicMock()
        codec.stop()
        codec.stop()  # Should not raise

    def test_stop_without_start(self, codec):
        """Calling stop() without start() should not raise."""
        codec.stop()  # bus is None, should be fine
