"""Sensor monitoring implementation."""

import asyncio
import logging
import random
import time
from enum import IntEnum
from typing import Any

import zelos_sdk
from zelos_sdk.actions import action

logger = logging.getLogger(__name__)


class State(IntEnum):
    """Monitor operational states."""

    IDLE = 0
    RUNNING = 1
    ERROR = 2


class SensorMonitor:
    """Monitors sensor data and streams to Zelos."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the sensor monitor.

        :param config: Configuration from config.json
        """
        self.config = config
        self.running = False
        self.state = State.IDLE
        self._start_time: float = 0.0

        # Create trace source with sensor name from config
        sensor_name = config.get("sensor_name", "zelos_extension_can")
        self.source = zelos_sdk.TraceSource(sensor_name)

        self._define_schema()

    def _define_schema(self) -> None:
        """Define trace event schemas with types and units."""
        # Monitor status - shows state and uptime
        self.source.add_event(
            "status",
            [
                zelos_sdk.TraceEventFieldMetadata("state", zelos_sdk.DataType.String),
                zelos_sdk.TraceEventFieldMetadata("uptime_s", zelos_sdk.DataType.Float64, "s"),
            ],
        )

        # Sensor readings - demonstrates proper types and units
        self.source.add_event(
            "sensor",
            [
                zelos_sdk.TraceEventFieldMetadata("temperature", zelos_sdk.DataType.Float32, "°C"),
                zelos_sdk.TraceEventFieldMetadata("humidity", zelos_sdk.DataType.Float32, "%"),
            ],
        )

    def start(self) -> None:
        """Start monitoring."""
        sensor_name = self.config.get("sensor_name", "sensor")
        logger.info(f"Starting {sensor_name}")
        self.running = True
        self.state = State.RUNNING
        self._start_time = time.time()

    def stop(self) -> None:
        """Stop monitoring."""
        logger.info("Stopping monitor")
        self.running = False
        self.state = State.IDLE
        self._start_time = 0.0

    def run(self) -> None:
        """Main run loop."""
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        """Main async loop - collect and stream sensor data."""
        self._start_time = time.time()

        while self.running:
            try:
                # Log current state and uptime
                uptime = time.time() - self._start_time
                self.source.status.log(state=self.state.name, uptime_s=round(uptime, 1))

                # Collect and log sensor readings
                temp = 20.0 + random.uniform(-5, 5)  # Simulate 15-25°C
                humidity = 50.0 + random.uniform(-15, 15)  # Simulate 35-65%

                self.source.sensor.log(
                    temperature=round(temp, 2),
                    humidity=round(humidity, 2),
                )

                # Wait for configured interval
                interval = self.config.get("interval", 0.1)
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                self.state = State.ERROR
                await asyncio.sleep(1)

    # --- Actions ---

    @action("Get Status", "View current status and uptime")
    def get_status(self) -> dict[str, Any]:
        """Get current status.

        :return: Status information
        """
        uptime = time.time() - self._start_time if self.running else 0.0
        return {
            "state": self.state.name,
            "running": self.running,
            "sensor_name": self.config.get("sensor_name", "unknown"),
            "interval": self.config.get("interval", 0.1),
            "uptime_s": round(uptime, 1),
        }

    @action("Set Interval", "Change sample rate")
    @action.number(
        "seconds",
        minimum=0.001,
        maximum=1.0,
        default=0.1,
        title="Interval (seconds)",
        widget="range",
    )
    def set_interval(self, seconds: float) -> dict[str, Any]:
        """Update the sample interval.

        :param seconds: New interval in seconds (0.001 to 1.0)
        :return: Confirmation
        """
        self.config["interval"] = seconds
        logger.info(f"Interval updated to {seconds}s")
        return {"message": f"Interval set to {seconds}s", "interval": seconds}
