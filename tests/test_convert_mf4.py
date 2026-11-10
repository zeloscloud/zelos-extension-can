"""MF4 and MDF convert through the action, unpatched: zelos-can's Rust reader, no asammdf."""

from pathlib import Path

import zelos_sdk

from zelos_extension_can import actions

FILES = Path(__file__).parent / "files"


def test_convert_mf4(tmp_path):
    dest = tmp_path / "pycan.trz"
    result = actions.convert(
        input_file=str(FILES / "pycan.mf4"),
        database_file=str(FILES / "test.dbc"),
        output_file=str(dest),
    )
    assert result["status"] == "success"
    assert dest.stat().st_size > 0


def test_convert_mdf_signal_recording_under_named_bus(tmp_path):
    dest = tmp_path / "signals.trz"
    result = actions.convert(
        input_file=str(FILES / "signals.mf4"), output_file=str(dest), bus="vehicle"
    )
    assert (result["signal_groups"], result["samples"]) == (2, 5)
    with zelos_sdk.TraceReader(str(dest)) as reader:
        paths = {f.path for s in reader.list_fields() for e in s.events for f in e.fields}
    assert "*/CAN/vehicle/Engine.gear" in paths
