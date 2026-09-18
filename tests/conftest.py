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


def trace_events(trz) -> dict[str, str | None]:
    """Every event in a trace: `source/event` path to declared event type."""
    with zelos_sdk.TraceReader(str(trz)) as reader:
        return {
            f"{source.name}/{event.name}": event.event_type
            for segment in reader.list_data_segments()
            for source in reader.list_fields(segment.id)
            for event in source.events
        }


def trace_event_paths(trz) -> set[str]:
    """The `source/event` path of every event in a trace."""
    return set(trace_events(trz))
