"""Tests for the sensor monitor."""

from typing import Any

from zelos_extension_can.extension import SensorMonitor, State


def test_monitor_init() -> None:
    """Test monitor initializes correctly."""
    config: dict[str, Any] = {"sensor_name": "test-sensor", "interval": 0.1}
    monitor = SensorMonitor(config)

    assert monitor.running is False
    assert monitor.state == State.IDLE
    assert monitor.config == config


def test_monitor_lifecycle() -> None:
    """Test monitor starts and stops correctly."""
    config: dict[str, Any] = {"sensor_name": "test-sensor", "interval": 0.1}
    monitor = SensorMonitor(config)

    assert monitor.state == State.IDLE
    assert monitor.running is False

    monitor.start()
    assert monitor.state == State.RUNNING
    assert monitor.running is True

    monitor.stop()
    assert monitor.state == State.IDLE
    assert monitor.running is False


def test_get_status_action(check) -> None:
    """Test get_status action returns correct info."""
    config: dict[str, Any] = {"sensor_name": "test-sensor", "interval": 0.1}
    monitor = SensorMonitor(config)

    monitor.start()
    status = monitor.get_status()

    check.that(status["state"], "==", "RUNNING")
    check.that(status["running"], "is", True)
    check.that(status["sensor_name"], "==", "test-sensor")
    check.that(status["interval"], "==", 0.1)
    check.that(status, "contains", "uptime_s")

    monitor.stop()
    status = monitor.get_status()
    check.that(status["state"], "==", "IDLE")
    check.that(status["running"], "is", False)


def test_get_status_before_start(check) -> None:
    """Test get_status action before start returns safe defaults."""
    config: dict[str, Any] = {"sensor_name": "test-sensor", "interval": 0.1}
    monitor = SensorMonitor(config)

    status = monitor.get_status()

    check.that(status["state"], "==", "IDLE")
    check.that(status["running"], "is", False)
    check.that(status["uptime_s"], "==", 0.0)


def test_set_interval_action(check) -> None:
    """Test set_interval action updates config."""
    config: dict[str, Any] = {"sensor_name": "test-sensor", "interval": 0.1}
    monitor = SensorMonitor(config)

    result = monitor.set_interval(0.5)

    check.that(result["interval"], "==", 0.5)
    check.that(monitor.config["interval"], "==", 0.5)
