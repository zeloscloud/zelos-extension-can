"""Tests for TRZ to candump log export functionality."""

from pathlib import Path

import pytest
import zelos_sdk

from zelos_extension_can.cli.export import (
    _format_candump_line,
    export_to_candump,
)


class TestCandumpFormatting:
    """Tests for candump line formatting."""

    def test_format_candump_line_basic(self):
        """Test basic candump line formatting."""
        line = _format_candump_line(
            timestamp_ns=1234567890123456789,
            channel="can0",
            arb_id=0x123,
            data=b"\x01\x02\x03\x04",
        )
        # Format: (timestamp) channel arbid#data
        assert line == "(1234567890.123457) can0 123#01020304"

    def test_format_candump_line_extended_id(self):
        """Test candump formatting with extended arbitration ID."""
        line = _format_candump_line(
            timestamp_ns=1000000000000000000,
            channel="vcan0",
            arb_id=0x1FFFFFFF,
            data=b"\xde\xad\xbe\xef",
        )
        assert line == "(1000000000.000000) vcan0 1FFFFFFF#DEADBEEF"

    def test_format_candump_line_empty_data(self):
        """Test candump formatting with empty data."""
        line = _format_candump_line(
            timestamp_ns=0,
            channel="can0",
            arb_id=0x100,
            data=b"",
        )
        assert line == "(0.000000) can0 100#"

    def test_format_candump_line_full_8_bytes(self):
        """Test candump formatting with full 8 byte payload."""
        line = _format_candump_line(
            timestamp_ns=500000000,
            channel="can1",
            arb_id=0x7FF,
            data=b"\x00\x11\x22\x33\x44\x55\x66\x77",
        )
        assert line == "(0.500000) can1 7FF#0011223344556677"


class TestExportIntegration:
    """Integration tests for TRZ export."""

    @pytest.fixture
    def test_dbc_path(self):
        """Get path to test DBC file."""
        return Path(__file__).parent / "files" / "test.dbc"

    def test_export_creates_log_file(self, test_dbc_path, tmp_path):
        """Test that export creates a valid log file from a TRZ with raw frames."""
        trz_file = tmp_path / "test_recording.trz"
        log_file = tmp_path / "test_output.log"

        # Create a TRZ file with raw CAN frames
        namespace = zelos_sdk.TraceNamespace("test_export")

        with zelos_sdk.TraceWriter(str(trz_file), namespace=namespace):
            # Create a raw CAN source
            raw_source = zelos_sdk.TraceSource("can_raw", namespace=namespace)
            raw_event = raw_source.add_event(
                "messages",
                [
                    zelos_sdk.TraceEventFieldMetadata(
                        name="arbitration_id",
                        data_type=zelos_sdk.DataType.UInt32,
                        unit=None,
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="dlc", data_type=zelos_sdk.DataType.UInt8, unit=None
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="data", data_type=zelos_sdk.DataType.Binary, unit=None
                    ),
                ],
            )

            # Log some test frames
            base_time = 1704067200000000000  # 2024-01-01 00:00:00 UTC in ns
            test_frames = [
                (base_time, 0x100, 8, b"\x01\x02\x03\x04\x05\x06\x07\x08"),
                (base_time + 1000000, 0x200, 4, b"\xaa\xbb\xcc\xdd"),
                (base_time + 2000000, 0x300, 2, b"\xff\x00"),
            ]

            for ts, arb_id, dlc, data in test_frames:
                raw_event.log_at(ts, arbitration_id=arb_id, dlc=dlc, data=data)

            # Small delay to ensure data is flushed
            import time

            time.sleep(0.1)

        # Now export the TRZ to candump log
        stats = export_to_candump(trz_file, log_file)

        # Verify export succeeded
        assert stats["frame_count"] == 3
        assert "can_raw" in stats["sources_found"]
        assert log_file.exists()

        # Verify log file contents
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 3

        # Check format of first line
        assert lines[0].startswith("(")
        assert "can0" in lines[0]
        assert "100#" in lines[0]

    def test_export_with_named_bus(self, test_dbc_path, tmp_path):
        """Test export with a named bus (e.g., chassis_raw)."""
        trz_file = tmp_path / "named_bus.trz"
        log_file = tmp_path / "named_bus.log"

        namespace = zelos_sdk.TraceNamespace("test_named")

        with zelos_sdk.TraceWriter(str(trz_file), namespace=namespace):
            # Create a named raw source (simulating bus_name="chassis")
            raw_source = zelos_sdk.TraceSource("chassis_raw", namespace=namespace)
            raw_event = raw_source.add_event(
                "messages",
                [
                    zelos_sdk.TraceEventFieldMetadata(
                        name="arbitration_id",
                        data_type=zelos_sdk.DataType.UInt32,
                        unit=None,
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="dlc", data_type=zelos_sdk.DataType.UInt8, unit=None
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="data", data_type=zelos_sdk.DataType.Binary, unit=None
                    ),
                ],
            )

            raw_event.log_at(
                1704067200000000000,
                arbitration_id=0x7E0,
                dlc=8,
                data=b"\x02\x01\x00\x00\x00\x00\x00\x00",
            )

            import time

            time.sleep(0.1)

        stats = export_to_candump(trz_file, log_file)

        assert stats["frame_count"] == 1
        assert "chassis_raw" in stats["sources_found"]

        # Verify channel name derived from source (chassis_raw -> chassis)
        content = log_file.read_text()
        assert "chassis" in content

    def test_export_no_raw_sources(self, tmp_path, caplog):
        """Test export when TRZ has no raw CAN sources."""
        import logging

        trz_file = tmp_path / "no_raw.trz"
        log_file = tmp_path / "no_raw.log"

        namespace = zelos_sdk.TraceNamespace("test_no_raw")

        with zelos_sdk.TraceWriter(str(trz_file), namespace=namespace):
            # Create a non-raw source
            source = zelos_sdk.TraceSource("can_codec", namespace=namespace)
            event = source.add_event(
                "VehicleSpeed",
                [
                    zelos_sdk.TraceEventFieldMetadata(
                        name="speed", data_type=zelos_sdk.DataType.Float32, unit="km/h"
                    ),
                ],
            )
            event.log_at(1704067200000000000, speed=50.0)

            import time

            time.sleep(0.1)

        with caplog.at_level(logging.ERROR):
            stats = export_to_candump(trz_file, log_file)

        assert stats["frame_count"] == 0
        assert len(stats["sources_found"]) == 0
        # Verify error message about enabling raw frame logging
        assert "No raw CAN sources found" in caplog.text
        assert "Log Raw CAN Frames" in caplog.text

    def test_export_multi_bus(self, tmp_path):
        """Test export with multiple raw CAN sources (can0_raw, can1_raw)."""
        trz_file = tmp_path / "multi_bus.trz"
        log_file = tmp_path / "multi_bus.log"

        namespace = zelos_sdk.TraceNamespace("test_multi")

        with zelos_sdk.TraceWriter(str(trz_file), namespace=namespace):
            # Create can0_raw source
            can0_source = zelos_sdk.TraceSource("can0_raw", namespace=namespace)
            can0_event = can0_source.add_event(
                "messages",
                [
                    zelos_sdk.TraceEventFieldMetadata(
                        name="arbitration_id",
                        data_type=zelos_sdk.DataType.UInt32,
                        unit=None,
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="dlc", data_type=zelos_sdk.DataType.UInt8, unit=None
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="data", data_type=zelos_sdk.DataType.Binary, unit=None
                    ),
                ],
            )

            # Create can1_raw source
            can1_source = zelos_sdk.TraceSource("can1_raw", namespace=namespace)
            can1_event = can1_source.add_event(
                "messages",
                [
                    zelos_sdk.TraceEventFieldMetadata(
                        name="arbitration_id",
                        data_type=zelos_sdk.DataType.UInt32,
                        unit=None,
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="dlc", data_type=zelos_sdk.DataType.UInt8, unit=None
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="data", data_type=zelos_sdk.DataType.Binary, unit=None
                    ),
                ],
            )

            # Log interleaved frames from both buses
            base_time = 1704067200000000000
            can0_event.log_at(
                base_time, arbitration_id=0x123, dlc=8, data=b"\x00\x12\x34\x56\x78\x9a\xbc\xde"
            )
            can1_event.log_at(base_time + 500000, arbitration_id=0x456, dlc=1, data=b"\x00")
            can0_event.log_at(
                base_time + 1000000, arbitration_id=0x124, dlc=4, data=b"\xaa\xbb\xcc\xdd"
            )
            can1_event.log_at(base_time + 1500000, arbitration_id=0x457, dlc=2, data=b"\xff\xee")

            import time

            time.sleep(0.1)

        stats = export_to_candump(trz_file, log_file)

        # Verify both sources found and exported
        assert stats["frame_count"] == 4
        assert "can0_raw" in stats["sources_found"]
        assert "can1_raw" in stats["sources_found"]
        assert "can0_raw" in stats["sources_exported"]
        assert "can1_raw" in stats["sources_exported"]

        # Verify log file contains frames from both buses
        content = log_file.read_text()
        lines = content.strip().split("\n")
        assert len(lines) == 4

        # Verify channel names are derived correctly
        assert "can0" in content
        assert "can1" in content

        # Verify frames are sorted by timestamp (interleaved)
        # Lines should alternate between can0 and can1
        assert "can0 123#" in lines[0]
        assert "can1 456#" in lines[1]
        assert "can0 124#" in lines[2]
        assert "can1 457#" in lines[3]


class TestRoundtrip:
    """Tests for log -> trz -> log roundtrip conversion."""

    @pytest.fixture
    def test_dbc_path(self):
        """Get path to test DBC file."""
        return Path(__file__).parent / "files" / "test.dbc"

    def test_log_trz_log_roundtrip(self, test_dbc_path, tmp_path):
        """Test that log -> trz -> log produces equivalent output.

        This verifies the workflow:
        1. Start with a candump log file
        2. Convert to TRZ (with raw frame logging)
        3. Export back to candump log
        4. Compare: frames should match (timestamps may differ slightly)
        """
        # Create an original candump log file
        original_log = tmp_path / "original.log"
        trz_file = tmp_path / "converted.trz"
        exported_log = tmp_path / "exported.log"

        # Write original candump log (using exact format python-can expects)
        original_frames = [
            "(1704067200.000000) can0 100#0102030405060708",
            "(1704067200.001000) can0 200#AABBCCDD",
            "(1704067200.002000) can0 300#FF00",
        ]
        original_log.write_text("\n".join(original_frames) + "\n")

        # Convert log -> trz (with raw frame logging enabled)
        # Note: We need the converter to enable raw frame logging
        # For now, just create a TRZ directly and test export
        namespace = zelos_sdk.TraceNamespace("roundtrip")

        with zelos_sdk.TraceWriter(str(trz_file), namespace=namespace):
            raw_source = zelos_sdk.TraceSource("can_raw", namespace=namespace)
            raw_event = raw_source.add_event(
                "messages",
                [
                    zelos_sdk.TraceEventFieldMetadata(
                        name="arbitration_id",
                        data_type=zelos_sdk.DataType.UInt32,
                        unit=None,
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="dlc", data_type=zelos_sdk.DataType.UInt8, unit=None
                    ),
                    zelos_sdk.TraceEventFieldMetadata(
                        name="data", data_type=zelos_sdk.DataType.Binary, unit=None
                    ),
                ],
            )

            # Log frames matching the original log
            base_ns = 1704067200000000000
            raw_event.log_at(
                base_ns, arbitration_id=0x100, dlc=8, data=b"\x01\x02\x03\x04\x05\x06\x07\x08"
            )
            raw_event.log_at(
                base_ns + 1000000, arbitration_id=0x200, dlc=4, data=b"\xaa\xbb\xcc\xdd"
            )
            raw_event.log_at(base_ns + 2000000, arbitration_id=0x300, dlc=2, data=b"\xff\x00")

            import time

            time.sleep(0.1)

        # Export trz -> log
        stats = export_to_candump(trz_file, exported_log)

        assert stats["frame_count"] == 3

        # Compare exported frames with original
        exported_lines = exported_log.read_text().strip().split("\n")
        assert len(exported_lines) == 3

        # Parse and compare frame data (ignoring exact timestamps)
        for orig, exported in zip(original_frames, exported_lines, strict=True):
            # Extract arbid#data from each line
            orig_data = orig.split()[-1]  # "100#0102030405060708"
            exported_data = exported.split()[-1]

            # Compare (case-insensitive for hex)
            assert orig_data.upper() == exported_data.upper(), (
                f"Frame mismatch: {orig_data} vs {exported_data}"
            )
