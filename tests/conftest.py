"""Shared test helpers."""

import time

import zelos_sdk


def wait_until(pred, timeout=4.0, interval=0.01):
    """Poll ``pred`` until truthy or timeout; return its final value."""
    deadline = time.monotonic() + timeout
    val = pred()
    while not val and time.monotonic() < deadline:
        time.sleep(interval)
        val = pred()
    return val


def trace_event_paths(trz) -> set[str]:
    """The `source/event` path of every event in a trace."""
    with zelos_sdk.TraceReader(str(trz)) as reader:
        return {
            f"{source.name}/{event.name}"
            for segment in reader.list_data_segments()
            for source in reader.list_fields(segment.id)
            for event in source.events
        }
