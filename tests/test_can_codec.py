"""Essential unit tests for CAN codec."""

from pathlib import Path
from unittest.mock import patch

import pytest

from zelos_extension_can.codec import CanCodec
from zelos_extension_can.schema_utils import cantools_signal_to_trace_type


@pytest.fixture
def test_dbc_path():
    """Path to test DBC file."""
    # Use test.dbc from tests/files directory
    test_files_dir = Path(__file__).parent / "files"
    return str(test_files_dir / "test.dbc")


@pytest.fixture
def mock_config(test_dbc_path):
    """Mock configuration."""
    return {
        "interface": "virtual",
        "channel": "vcan0",
        "bitrate": 500000,
        "dbc_file": test_dbc_path,
    }


@pytest.fixture
def codec(mock_config):
    """Create CanCodec instance."""
    with patch("zelos_sdk.TraceSource"):
        return CanCodec(mock_config)


class TestCanCodecInitialization:
    """Test codec initialization and setup."""

    def test_loads_dbc(self, codec, test_dbc_path):
        """Test DBC file is loaded."""
        assert codec.db is not None
        assert len(codec.db.messages) == 13  # test.dbc has 13 messages

    def test_creates_message_lookups(self, codec):
        """Test message lookup dictionaries are populated."""
        assert len(codec.messages_by_id) > 0
        assert len(codec.messages_by_name) > 0

    def test_handles_duplicate_message_names(self, codec):
        """Test duplicate message names are handled gracefully."""
        # test.dbc has duplicate "Duplicate_Message" entries
        # Should only keep first one in messages_by_name
        duplicate_count = sum(1 for msg in codec.db.messages if msg.name == "Duplicate_Message")
        assert duplicate_count == 2
        assert "Duplicate_Message" in codec.messages_by_name

    def test_generates_event_names(self, codec):
        """Test event name generation format."""
        msg = codec.db.get_message_by_name("DUT_Status")
        event_name = codec._get_event_name(msg)
        assert event_name == "0064_DUT_Status"  # 0x64 = 100


class TestSchemaUtils:
    """Test DBC to SDK type mapping."""

    def test_float_signal_mapping(self, codec):
        """Test float signal maps to Float32/Float64."""
        # Use real signal from DBC
        msg = codec.db.get_message_by_name("DUT_Status")
        float_signal = msg.get_signal_by_name("float_signal")

        from zelos_sdk import DataType

        result = cantools_signal_to_trace_type(float_signal)
        assert result == DataType.Float32

    def test_integer_signal_mapping(self, codec):
        """Test integer signal maps correctly."""
        # Use real signal from DBC
        msg = codec.db.get_message_by_name("DUT_Status")
        state_signal = msg.get_signal_by_name("state")  # 2-bit unsigned

        from zelos_sdk import DataType

        result = cantools_signal_to_trace_type(state_signal)
        assert result == DataType.UInt8

    def test_signed_integer_mapping(self, codec):
        """Test signed integer mapping."""
        # Use real signal from DBC
        msg = codec.db.get_message_by_name("DUT_Status")
        signed_signal = msg.get_signal_by_name("signed_signal")  # 2-bit signed

        from zelos_sdk import DataType

        result = cantools_signal_to_trace_type(signed_signal)
        assert result == DataType.Int8


class TestMessageDecoding:
    """Test CAN message decoding."""

    def test_get_event_name_format(self, codec):
        """Test event names follow {id:04x}_{name} or {id:08x}_{name} format for extended IDs."""
        for msg in codec.db.messages:
            event_name = codec._get_event_name(msg)
            assert "_" in event_name
            msg_id_hex, msg_name = event_name.split("_", 1)
            # Standard IDs (11-bit) use 4 hex chars, Extended IDs (29-bit) use 8 hex chars
            assert len(msg_id_hex) in [4, 8]
            assert int(msg_id_hex, 16) == msg.frame_id


class TestConfiguration:
    """Test configuration handling."""

    def test_requires_interface(self, test_dbc_path):
        """Test interface is required."""
        config = {"channel": "can0", "dbc_file": test_dbc_path}
        with pytest.raises(KeyError), patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)
            codec.start()

    def test_requires_channel(self, test_dbc_path):
        """Test channel is required."""
        config = {"interface": "virtual", "dbc_file": test_dbc_path}
        with pytest.raises(KeyError), patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)
            codec.start()

    def test_bitrate_optional_for_virtual(self, mock_config):
        """Test bitrate is optional for virtual interface."""
        del mock_config["bitrate"]
        with patch("zelos_sdk.TraceSource"):
            CanCodec(mock_config)
            # Should not raise


class TestConfigJsonMerging:
    """Test config_json merging functionality."""

    def test_config_json_merges_with_bus_config(self, mock_config):
        """Test config_json is merged into bus config."""
        mock_config["config_json"] = '{"app_name": "TestApp", "receive_own_messages": false}'

        with patch("zelos_sdk.TraceSource"), patch("can.Bus") as mock_bus:
            codec = CanCodec(mock_config)
            codec.start()

            # Verify Bus was called with merged config
            call_kwargs = mock_bus.call_args.kwargs
            assert call_kwargs["app_name"] == "TestApp"
            assert call_kwargs["receive_own_messages"] is False  # Overridden
            assert call_kwargs["interface"] == "virtual"
            assert call_kwargs["channel"] == "vcan0"

    def test_config_json_empty_string_ignored(self, mock_config):
        """Test empty config_json is ignored."""
        mock_config["config_json"] = ""

        with patch("zelos_sdk.TraceSource"), patch("can.Bus") as mock_bus:
            codec = CanCodec(mock_config)
            codec.start()

            # Should work normally without config_json
            call_kwargs = mock_bus.call_args.kwargs
            assert "app_name" not in call_kwargs


class TestTimestampHandling:
    """Test timestamp handling modes."""

    def test_timestamp_mode_auto_boot_relative(self, mock_config):
        """Test auto mode detects boot-relative timestamps."""
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)

            # First timestamp is small (< 1 hour) - should be detected as boot-relative
            first_hw_ts = 15.5  # 15.5 seconds since boot
            timestamp_ns = codec.get_timestamp(first_hw_ts)

            assert codec.hw_timestamp_offset is not None
            assert codec.hw_timestamp_offset > 0
            assert timestamp_ns is not None
            # Result should be close to current time
            import time

            expected_ns = time.time() * 1e9
            assert abs(timestamp_ns - expected_ns) < 1e9  # Within 1 second

    def test_timestamp_mode_auto_absolute(self, mock_config):
        """Test auto mode detects absolute wall-clock timestamps."""
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)

            # First timestamp is large (> 1 hour) - should be detected as absolute
            import time

            first_hw_ts = time.time()  # Current wall-clock time
            timestamp_ns = codec.get_timestamp(first_hw_ts)

            assert codec.hw_timestamp_offset == 0.0
            assert timestamp_ns is not None
            assert timestamp_ns == int(first_hw_ts * 1e9)

    def test_timestamp_mode_absolute(self, mock_config):
        """Test absolute mode uses timestamps as-is."""
        mock_config["timestamp_mode"] = "absolute"
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)

            # Small timestamp - should still use as-is
            hw_ts = 15.5
            timestamp_ns = codec.get_timestamp(hw_ts)

            assert timestamp_ns == int(hw_ts * 1e9)
            assert codec.hw_timestamp_offset is None  # Not set in absolute mode

    def test_timestamp_mode_ignore(self, mock_config):
        """Test ignore mode returns None to use system time."""
        mock_config["timestamp_mode"] = "ignore"
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)

            hw_ts = 15.5
            timestamp_ns = codec.get_timestamp(hw_ts)

            assert timestamp_ns is None

    def test_timestamp_mode_none_hw_timestamp(self, mock_config):
        """Test handling of None hardware timestamp."""
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)

            timestamp_ns = codec.get_timestamp(None)
            assert timestamp_ns is None

    def test_timestamp_mode_auto_consistent_offset(self, mock_config):
        """Test auto mode applies consistent offset to subsequent timestamps."""
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)

            # First timestamp establishes offset
            first_hw_ts = 10.0
            timestamp_ns1 = codec.get_timestamp(first_hw_ts)
            offset = codec.hw_timestamp_offset

            # Second timestamp should use same offset
            second_hw_ts = 20.0
            timestamp_ns2 = codec.get_timestamp(second_hw_ts)

            # Verify offset is preserved
            assert codec.hw_timestamp_offset == offset
            # Verify the time difference is preserved
            assert (timestamp_ns2 - timestamp_ns1) == int((second_hw_ts - first_hw_ts) * 1e9)

    def test_message_handling_with_boot_relative_timestamps(self, mock_config):
        """Test full message handling flow with boot-relative timestamps."""
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)

            # Create a mock CAN message with boot-relative timestamp
            import can

            msg = can.Message(
                arbitration_id=0x64,  # DUT_Status message ID from test.dbc
                data=bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
                timestamp=15.5,  # Boot-relative: 15.5 seconds since boot
            )

            # Handle the message
            codec._handle_message(msg)

            # Verify timestamp was processed correctly
            assert codec.hw_timestamp_offset is not None
            assert codec.hw_timestamp_offset > 0
            assert codec.first_hw_timestamp == 15.5

            # Create second message with later timestamp
            msg2 = can.Message(
                arbitration_id=0x64,
                data=bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
                timestamp=16.5,  # 1 second later
            )

            # Handle second message
            codec._handle_message(msg2)

            # Verify offset remained the same
            assert codec.first_hw_timestamp == 15.5  # Should not change
            # Offset should be consistent
            import time

            expected_offset = time.time() - 15.5
            assert abs(codec.hw_timestamp_offset - expected_offset) < 2.0  # Within 2 seconds

    def test_message_handling_with_absolute_timestamps(self, mock_config):
        """Test full message handling flow with absolute wall-clock timestamps."""
        mock_config["timestamp_mode"] = "absolute"
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)

            # Create a mock CAN message with absolute timestamp
            import time

            import can

            wall_clock_time = time.time()
            msg = can.Message(
                arbitration_id=0x64,  # DUT_Status message ID
                data=bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
                timestamp=wall_clock_time,
            )

            # Handle the message
            codec._handle_message(msg)

            # In absolute mode, offset should not be set
            assert codec.hw_timestamp_offset is None

    def test_message_handling_preserves_relative_timing(self, mock_config):
        """Test that relative timing between messages is preserved."""
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)

            # Create sequence of messages with boot-relative timestamps

            base_time = 100.0  # 100 seconds since boot
            timestamps = [base_time, base_time + 0.1, base_time + 0.2, base_time + 0.5]

            processed_timestamps = []
            for ts in timestamps:
                # Get the converted timestamp
                converted_ts = codec.get_timestamp(ts)
                processed_timestamps.append(converted_ts)

            # Verify relative timing is preserved
            for i in range(1, len(timestamps)):
                original_delta = (timestamps[i] - timestamps[i - 1]) * 1e9  # Convert to ns
                processed_delta = processed_timestamps[i] - processed_timestamps[i - 1]
                assert abs(original_delta - processed_delta) < 1000  # Within 1 microsecond


class TestActions:
    """Test action methods."""

    def test_get_status(self, codec):
        """Test get_status action returns expected fields."""
        status = codec.get_status()
        assert "running" in status
        assert "interface" in status
        assert "channel" in status
        assert "messages_in_dbc" in status
        assert status["messages_in_dbc"] == 13
        assert "fd_mode" in status

    def test_get_bus_health(self, codec):
        """Test get_bus_health action."""
        result = codec.get_bus_health()
        assert "error" in result  # Bus not initialized yet

    def test_get_metrics(self, codec):
        """Test get_metrics action."""
        metrics = codec.get_metrics()
        assert "messages_received" in metrics
        assert "messages_decoded" in metrics
        assert "decode_errors" in metrics
        assert "unknown_messages" in metrics
        assert "uptime_seconds" in metrics
        assert "messages_per_second" in metrics
        assert metrics["messages_received"] == 0

    def test_list_periodic_tasks(self, codec):
        """Test list_periodic_tasks action."""
        result = codec.list_periodic_tasks()
        assert "count" in result
        assert "tasks" in result
        assert result["count"] == 0

    def test_list_messages(self, codec):
        """Test list_messages action."""
        result = codec.list_messages()
        assert "count" in result
        assert "messages" in result
        assert result["count"] == 13
        assert len(result["messages"]) == 13

        # Check message format
        first_msg = result["messages"][0]
        assert "id" in first_msg
        assert "name" in first_msg
        assert "length" in first_msg
        assert "signals" in first_msg


class TestErrorHandling:
    """Test error handling and resilience."""

    def test_handles_missing_dbc_file(self, test_dbc_path):
        """Test proper error when DBC file doesn't exist."""
        config = {"interface": "virtual", "channel": "vcan0", "dbc_file": "/nonexistent/file.dbc"}
        with (
            pytest.raises(FileNotFoundError, match="DBC file not found"),
            patch("zelos_sdk.TraceSource"),
        ):
            CanCodec(config)

    def test_handles_invalid_dbc_file(self, tmp_path):
        """Test proper error when DBC file is invalid."""
        bad_dbc = tmp_path / "bad.dbc"
        bad_dbc.write_text("not a valid dbc file")

        config = {"interface": "virtual", "channel": "vcan0", "dbc_file": str(bad_dbc)}
        with (
            pytest.raises(ValueError, match="Failed to load DBC file"),
            patch("zelos_sdk.TraceSource"),
        ):
            CanCodec(config)

    def test_send_message_with_extended_id(self, codec):
        """Test sending message with extended ID validation."""
        # Standard ID within range
        result = codec.send_message(0x100, "01 02", extended_id=False)
        assert "error" in result  # Bus not started

        # Extended ID validation
        result = codec.send_message(0x1FFFFFFF, "01 02", extended_id=True)
        assert "error" in result  # Bus not started, but ID validated


class TestConfigValidation:
    """Test configuration validation."""

    def test_validate_missing_required_fields(self):
        """Test validation catches missing required fields."""
        from zelos_extension_can.utils.config import validate_config

        config = {}
        errors = validate_config(config)
        assert len(errors) == 3
        assert any("interface" in e for e in errors)
        assert any("channel" in e for e in errors)
        assert any("dbc_file" in e for e in errors)

    def test_validate_missing_dbc_file(self):
        """Test validation catches missing DBC file."""
        from zelos_extension_can.utils.config import validate_config

        config = {"interface": "virtual", "channel": "vcan0", "dbc_file": "/nonexistent.dbc"}
        errors = validate_config(config)
        assert any("DBC file not found" in e for e in errors)

    def test_validate_invalid_bitrate(self, test_dbc_path):
        """Test validation catches invalid bitrate."""
        from zelos_extension_can.utils.config import validate_config

        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "dbc_file": test_dbc_path,
            "bitrate": 999999,
        }
        errors = validate_config(config)
        assert any("Invalid bitrate" in e for e in errors)

    def test_validate_socketcan_channel(self, test_dbc_path):
        """Test validation of socketcan channel naming."""
        from zelos_extension_can.utils.config import validate_config

        config = {
            "interface": "socketcan",
            "channel": "invalid_name",
            "dbc_file": test_dbc_path,
        }
        errors = validate_config(config)
        assert any("socketcan interface requires" in e for e in errors)

    def test_validate_bitrate_optional_for_virtual(self, test_dbc_path):
        """Test bitrate is optional for virtual interface."""
        from zelos_extension_can.utils.config import validate_config

        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "dbc_file": test_dbc_path,
            # No bitrate
        }
        errors = validate_config(config)
        assert len(errors) == 0  # Should pass without bitrate

    def test_validate_bitrate_optional_for_socketcan(self, test_dbc_path):
        """Test bitrate is optional for socketcan interface."""
        from zelos_extension_can.utils.config import validate_config

        config = {
            "interface": "socketcan",
            "channel": "can0",
            "dbc_file": test_dbc_path,
            # No bitrate
        }
        errors = validate_config(config)
        assert len(errors) == 0  # Should pass without bitrate

    def test_validate_bitrate_required_for_pcan(self, test_dbc_path):
        """Test bitrate is required for hardware interfaces like PCAN."""
        from zelos_extension_can.utils.config import validate_config

        config = {
            "interface": "pcan",
            "channel": "PCAN_USBBUS1",
            "dbc_file": test_dbc_path,
            # No bitrate
        }
        errors = validate_config(config)
        assert any("Bitrate is required" in e for e in errors)

    def test_validate_data_url(self):
        """Test data-url validation works correctly."""
        from zelos_extension_can.utils.config import validate_config

        # Valid base64-encoded data-url
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "dbc_file": "data:application/octet-stream;base64,VkVSU0lPTiA=",  # "VERSION "
        }
        errors = validate_config(config)
        assert len(errors) == 0  # Should accept valid data-url

    def test_validate_timestamp_mode(self):
        """Test timestamp_mode validation."""
        from zelos_extension_can.utils.config import validate_config

        # Valid timestamp modes
        for mode in ["auto", "absolute", "ignore"]:
            config = {
                "interface": "virtual",
                "channel": "vcan0",
                "dbc_file": "/path/to/file.dbc",
                "timestamp_mode": mode,
            }
            errors = validate_config(config)
            # Should have one error (file not found), but not timestamp_mode error
            assert not any("timestamp_mode" in e for e in errors)

        # Invalid timestamp mode
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "dbc_file": "/path/to/file.dbc",
            "timestamp_mode": "invalid_mode",
        }
        errors = validate_config(config)
        assert any("Invalid timestamp_mode" in e for e in errors)
        assert any("invalid_mode" in e for e in errors)

    def test_validate_config_json(self):
        """Test config_json validation."""
        from zelos_extension_can.utils.config import validate_config

        # Valid JSON object
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "dbc_file": "/path/to/file.dbc",
            "config_json": '{"app_name": "MyApp", "rx_queue_size": 1000}',
        }
        errors = validate_config(config)
        # Should have one error (file not found), but not config_json error
        assert not any("config_json" in e for e in errors)

        # Invalid JSON syntax
        config["config_json"] = '{"app_name": "MyApp",}'  # Trailing comma
        errors = validate_config(config)
        assert any("Invalid JSON in config_json" in e for e in errors)

        # JSON array instead of object
        config["config_json"] = '["value1", "value2"]'
        errors = validate_config(config)
        assert any("config_json must be a JSON object" in e for e in errors)

        # Empty string is valid (ignored)
        config["config_json"] = ""
        errors = validate_config(config)
        assert not any("config_json" in e for e in errors)


class TestConfigUtils:
    """Test configuration utility functions."""

    def test_data_url_to_file(self, tmp_path):
        """Test data-url to file conversion."""
        from zelos_extension_can.utils.config import data_url_to_file

        # Create a simple data-url (base64 encoded "test content")
        data_url = "data:text/plain;base64,dGVzdCBjb250ZW50"
        output_path = tmp_path / "test.txt"

        result = data_url_to_file(data_url, str(output_path))

        assert result == str(output_path)
        assert output_path.exists()
        assert output_path.read_text() == "test content"

    def test_get_platform_defaults(self):
        """Test platform defaults are returned."""
        from zelos_extension_can.utils.config import get_platform_defaults

        defaults = get_platform_defaults()
        assert "interface" in defaults
        # Now defaults to demo mode, no channel needed
        assert defaults["interface"] == "demo"
