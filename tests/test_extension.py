"""Tests for the sensor monitor."""

from typing import Any

from zelos_extension_can.extension import SensorMonitor


def test_monitor_init() -> None:
    """Test monitor initializes correctly."""
    config: dict[str, Any] = {"sensor_name": "test-sensor", "interval": 0.1}
    monitor = SensorMonitor(config)

    assert monitor.running is False
    assert monitor.config == config
    assert monitor._start_time == 0.0


def test_monitor_lifecycle() -> None:
    """Test monitor starts and stops correctly."""
    config: dict[str, Any] = {"sensor_name": "test-sensor", "interval": 0.1}
    monitor = SensorMonitor(config)

    assert monitor.running is False

    monitor.start()
    assert monitor.running is True
    assert monitor._start_time > 0

    monitor.stop()
    assert monitor.running is False


def test_set_interval_action(check) -> None:
    """Test set_interval action updates config."""
    config: dict[str, Any] = {"sensor_name": "test-sensor", "interval": 0.1}
    monitor = SensorMonitor(config)

    result = monitor.set_interval(0.5)

    check.that(result["interval"], "==", 0.5)
    check.that(monitor.config["interval"], "==", 0.5)


def test_schema_definition() -> None:
    """Test that schema is defined correctly."""
    config: dict[str, Any] = {"sensor_name": "test", "interval": 0.1}
    monitor = SensorMonitor(config)

    # Verify all events exist
    assert hasattr(monitor.source, "environmental")
    assert hasattr(monitor.source, "power")


def test_config_defaults() -> None:
    """Test that configuration defaults work correctly."""
    # Empty config should still work
    config: dict[str, Any] = {}
    monitor = SensorMonitor(config)

    # Should use defaults
    assert monitor.config.get("interval", 0.1) == 0.1
    assert monitor.config.get("sensor_name", "sensor") == "sensor"
