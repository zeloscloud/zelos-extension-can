"""Shared test helpers."""

import pytest
import zelos_sdk


@pytest.fixture
def trace_event_paths():
    """Return a reader for the `source/event` path of every event in a trace."""

    def _paths(trz) -> set[str]:
        with zelos_sdk.TraceReader(str(trz)) as reader:
            return {
                f"{source.name}/{event.name}"
                for segment in reader.list_data_segments()
                for source in reader.list_fields(segment.id)
                for event in source.events
            }

    return _paths
