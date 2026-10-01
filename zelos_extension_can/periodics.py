"""Periodic transmit: one task per slot, kept sending through bus faults and
started again on a reopened bus."""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import can

logger = logging.getLogger(__name__)


class RepeatFilter(logging.Filter):
    """Pass a record once per window per message, so a periodic on a stalled bus
    logs its failure once rather than every period. A CAN error is the bus
    failing, not a bug, so it goes without its traceback."""

    def __init__(self, window_s: float) -> None:
        super().__init__()
        self._window_s = window_s
        self._last: dict[str, float] = {}
        # Every periodic's thread logs through this filter.
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        now = time.monotonic()
        key = record.getMessage()
        with self._lock:
            if len(self._last) > 64:
                self._last = {k: t for k, t in self._last.items() if now - t < self._window_s}
            last = self._last.get(key)
            if last is not None and now - last < self._window_s:
                return False
            self._last[key] = now
        if record.exc_info and isinstance(record.exc_info[1], can.CanError):
            record.exc_info = record.exc_text = None
        return True


logging.getLogger("can.bcm").addFilter(RepeatFilter(60.0))


class Periodics:
    """A bus's periodic transmits: the task in each slot, what it sends, and
    the spec to start it again on a reopened bus.

    Not locked: the codec serializes these calls with opening, reopening and
    stopping its bus."""

    def __init__(self, bus_name: str, on_failure: Callable[[Exception], None]) -> None:
        """
        :param on_failure: Called with each send a periodic's bus fails.
        """
        self.bus_name = bus_name
        self._on_failure = on_failure
        # python-can's CyclicSendTask. Owns its own thread, exposes `.stop()`
        # and `.modify_data()`; we don't manage an asyncio loop here because
        # action dispatch happens in worker threads where `asyncio.create_task`
        # raises "no running event loop".
        self.tasks: dict[str, can.broadcastmanager.CyclicSendTaskABC] = {}
        # Slot metadata so get_tx_state can reconstruct what each task is
        # sending without poking the task object's internals.
        self.slots: dict[str, dict[str, Any]] = {}
        # (message, period_s, mode) per task, to start it again on a reopened bus.
        self.specs: dict[str, tuple[can.Message, float, str]] = {}

    def start(
        self,
        bus: Any,
        tid: str,
        msg: can.Message,
        period_s: float,
        mode: str,
        slot: dict[str, Any] | None = None,
    ) -> None:
        # python-can's thread-based task ends for good on its first failed send
        # unless it has an `on_error`, which only a task not yet started takes
        # without a race; a bus that starts its own gets it right after.
        old = self.tasks.pop(tid, None)
        if old is not None:
            old.stop()
        own = getattr(type(bus), "_send_periodic_internal", None)
        threaded = own is can.BusABC._send_periodic_internal
        task = bus.send_periodic(msg, period_s, autostart=not threaded)
        if isinstance(task, can.broadcastmanager.ThreadBasedCyclicSendTask):
            task.on_error = lambda exc: self._on_error(tid, exc)
        # Recorded before the task starts, so a first send that fails can mark it.
        if slot is not None:
            self.slots[tid] = slot
        elif tid in self.slots:
            self.slots[tid]["is_active"] = True
        if threaded:
            task.start()
        self.tasks[tid] = task
        self.specs[tid] = (msg, period_s, mode)
        logger.info("started periodic %s mode=%s period=%.3fs", tid, mode, period_s)

    def stop(self, tid: str) -> bool:
        task = self.tasks.pop(tid, None)
        self.slots.pop(tid, None)
        self.specs.pop(tid, None)
        if task is None:
            return False
        task.stop()
        logger.info("stopped periodic %s", tid)
        return True

    def stop_all(self) -> None:
        for tid, task in list(self.tasks.items()):
            logger.info("Stopping periodic task: %s", tid)
            task.stop()
        self.tasks.clear()
        self.slots.clear()
        self.specs.clear()

    def halt(self) -> None:
        """Stop every task, keeping each spec to start it again."""
        for task in self.tasks.values():
            task.stop()
        self.tasks.clear()

    def forget_tasks(self) -> None:
        """Drop the tasks a bus took down with it on shutdown, keeping each spec."""
        self.tasks.clear()

    def rearm(self, bus: Any) -> None:
        """Start every periodic again on a reopened bus. One that fails is
        logged and kept, to try again on the next reopen."""
        for tid, (msg, period_s, mode) in list(self.specs.items()):
            try:
                self.start(bus, tid, msg, period_s, mode)
            except Exception as e:
                logger.error("[%s] Could not restart periodic %s: %s", self.bus_name, tid, e)
                continue
            logger.info("[%s] re-armed periodic %s", self.bus_name, tid)

    def snapshot(self) -> list[dict[str, Any]]:
        return [self.slots[tid] for tid in sorted(self.slots)]

    def _on_error(self, tid: str, exc: Exception) -> bool:
        """Count a failed period and keep the task while the bus is at fault:
        its frames resume once the bus drains, where python-can's default would
        end it for good. Any other error ends it, marked inactive."""
        self._on_failure(exc)
        if isinstance(exc, can.CanOperationError):
            return True
        logger.error("[%s] Periodic %s stopped: %s", self.bus_name, tid, exc)
        slot = self.slots.get(tid)
        if slot is not None:
            slot["is_active"] = False
        return False
