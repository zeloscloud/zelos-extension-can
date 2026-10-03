"""MF4 converts through the action, unpatched: zelos-can's Rust reader, no asammdf."""

from pathlib import Path

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
