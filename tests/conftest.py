"""Shared test helpers."""

import zelos_sdk


def trace_event_paths(trz) -> set[str]:
    """The `source/event` path of every event in a trace."""
    with zelos_sdk.TraceReader(str(trz)) as reader:
        return {
            f"{source.name}/{event.name}"
            for segment in reader.list_data_segments()
            for source in reader.list_fields(segment.id)
            for event in source.events
        }
