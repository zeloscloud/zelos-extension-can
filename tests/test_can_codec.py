"""Essential unit tests for CAN codec."""

from pathlib import Path
from unittest.mock import patch

import pytest

from zelos_extension_can.codec import CanCodec, TimestampMode
from zelos_extension_can.utils.schema_utils import cantools_signal_to_trace_type


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
        "database_file": test_dbc_path,
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

    def test_caches_event_loggers_on_first_message(self, codec):
        """Test that events are lazily generated on first message (default behavior)."""
        import can

        # By default, events are not pre-generated (emit_all_schemas_on_init=false)
        assert len(codec._events) == 0

        # Event should not exist before first message
        assert 0x64 not in codec._events

        msg = can.Message(
            arbitration_id=0x64,
            data=bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
            timestamp=15.5,
        )

        # Handle the message - should generate schema lazily
        codec._handle_message(msg)

        # Now the event should exist
        assert 0x64 in codec._events
        assert codec._events[0x64] is not None

        # Handling the same message again should not increase cache size
        cache_size_after_first = len(codec._events)
        codec._handle_message(msg)
        assert len(codec._events) == cache_size_after_first

    def test_emit_all_schemas_on_init(self, mock_config):
        """Test that all schemas are pre-generated when emit_schemas_on_init=true."""
        from unittest.mock import patch

        import can

        # Set config to pre-generate all schemas
        mock_config["emit_schemas_on_init"] = True

        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)

        # All events should be pre-generated at init
        assert len(codec._events) > 0

        # Specific event should exist
        assert 0x64 in codec._events
        assert codec._events[0x64] is not None

        msg = can.Message(
            arbitration_id=0x64,
            data=bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
            timestamp=15.5,
        )

        # Handling message should not change cache size (already pre-generated)
        initial_cache_size = len(codec._events)
        codec._handle_message(msg)
        assert len(codec._events) == initial_cache_size

    def test_timestamp_mode_enum_conversion(self, mock_config):
        """Test timestamp_mode string is converted to enum."""
        mock_config["timestamp_mode"] = "auto"
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)
            assert codec.timestamp_mode == TimestampMode.AUTO
            assert isinstance(codec.timestamp_mode, TimestampMode)

        mock_config["timestamp_mode"] = "absolute"
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)
            assert codec.timestamp_mode == TimestampMode.ABSOLUTE

        mock_config["timestamp_mode"] = "ignore"
        with patch("zelos_sdk.TraceSource"):
            codec = CanCodec(mock_config)
            assert codec.timestamp_mode == TimestampMode.IGNORE

    def test_inherits_can_listener(self, codec):
        """Test codec inherits from can.Listener for direct callbacks."""
        import can

        assert isinstance(codec, can.Listener)
        assert hasattr(codec, "on_message_received")
        assert callable(codec.on_message_received)


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
        config = {"channel": "can0", "database_file": test_dbc_path}
        with pytest.raises(KeyError), patch("zelos_sdk.TraceSource"):
            codec = CanCodec(config)
            codec.start()

    def test_requires_channel(self, test_dbc_path):
        """Test channel is required."""
        config = {"interface": "virtual", "database_file": test_dbc_path}
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
        assert "bus_state" in status
        assert "fd_mode" in status

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
        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "database_file": "/nonexistent/file.dbc",
        }
        with (
            pytest.raises(FileNotFoundError, match="CAN database file not found"),
            patch("zelos_sdk.TraceSource"),
        ):
            CanCodec(config)

    def test_handles_invalid_dbc_file(self, tmp_path):
        """Test proper error when DBC file is invalid."""
        bad_dbc = tmp_path / "bad.dbc"
        bad_dbc.write_text("not a valid dbc file")

        config = {"interface": "virtual", "channel": "vcan0", "database_file": str(bad_dbc)}
        with (
            pytest.raises(ValueError, match="Failed to load database file"),
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


class TestFileUtils:
    """Test file utility functions."""

    def test_data_url_to_file(self, tmp_path):
        """Test data-url to file conversion."""
        from zelos_extension_can.utils.file_utils import data_url_to_file

        # Create a simple data-url (base64 encoded "test content")
        data_url = "data:text/plain;base64,dGVzdCBjb250ZW50"
        output_path = tmp_path / "test.txt"

        result = data_url_to_file(data_url, str(output_path))

        assert result == str(output_path)
        assert output_path.exists()
        assert output_path.read_text() == "test content"


class TestMultiBusSupport:
    """Test multi-bus configuration support."""

    def test_codec_with_bus_name_uses_exact_name(self, mock_config):
        """Test that bus_name is used as exact trace source name."""
        with patch("zelos_sdk.TraceSource") as mock_source:
            CanCodec(mock_config, bus_name="powertrain")
            # Verify trace source created with exact name
            mock_source.assert_any_call("powertrain")

    def test_codec_without_bus_name_uses_default(self, mock_config):
        """Test that no bus_name uses default trace source name."""
        with patch("zelos_sdk.TraceSource") as mock_source:
            CanCodec(mock_config)
            mock_source.assert_any_call("can_codec")

    def test_codec_with_bus_name_raw_source(self, mock_config):
        """Test that bus_name is used for raw trace source when enabled."""
        mock_config["log_raw_frames"] = True
        with patch("zelos_sdk.TraceSource") as mock_source:
            CanCodec(mock_config, bus_name="chassis")
            calls = [str(c) for c in mock_source.call_args_list]
            assert any("'chassis'" in c for c in calls)
            assert any("'chassis_raw'" in c for c in calls)

    def test_prepare_bus_config_demo_mode(self, test_dbc_path):
        """Test _prepare_bus_config handles demo interface."""
        from zelos_extension_can.cli.app import _prepare_bus_config

        bus_config = {"name": "demo", "interface": "demo"}
        demo_dbc = Path(test_dbc_path)

        result = _prepare_bus_config(bus_config, demo_dbc)

        assert result["interface"] == "virtual"
        assert result["channel"] == "vcan0"
        assert result["demo_mode"] is True
        assert result["receive_own_messages"] is True

    def test_prepare_bus_config_passthrough(self, test_dbc_path):
        """Test _prepare_bus_config passes through normal config."""
        from zelos_extension_can.cli.app import _prepare_bus_config

        bus_config = {
            "name": "can0",
            "interface": "socketcan",
            "channel": "can0",
            "database_file": test_dbc_path,
        }

        result = _prepare_bus_config(bus_config, Path(test_dbc_path))

        assert result["interface"] == "socketcan"
        assert result["channel"] == "can0"
        assert "demo_mode" not in result

    def test_single_bus_no_name_backward_compatible(self, test_dbc_path):
        """Test single bus without name uses default 'can_codec' (backward compatible)."""
        from zelos_extension_can.cli.app import _create_codecs

        config = {
            "buses": [{"interface": "virtual", "channel": "vcan0", "database_file": test_dbc_path}]
        }

        with patch("zelos_sdk.TraceSource"):
            codecs = _create_codecs(config, Path(test_dbc_path))

        assert len(codecs) == 1
        codec, action_name = codecs[0]
        assert action_name == "can_codec"
        assert codec.bus_name is None

    def test_multi_bus_defaults_name_to_channel(self, test_dbc_path):
        """Test multi-bus without names defaults to channel names."""
        from zelos_extension_can.cli.app import _create_codecs

        config = {
            "buses": [
                {"interface": "virtual", "channel": "vcan0", "database_file": test_dbc_path},
                {"interface": "virtual", "channel": "vcan1", "database_file": test_dbc_path},
            ]
        }

        with patch("zelos_sdk.TraceSource"):
            codecs = _create_codecs(config, Path(test_dbc_path))

        assert len(codecs) == 2
        assert codecs[0][0].bus_name == "vcan0"
        assert codecs[0][1] == "vcan0"
        assert codecs[1][0].bus_name == "vcan1"
        assert codecs[1][1] == "vcan1"

    def test_multi_bus_rejects_duplicate_names(self, test_dbc_path):
        """Test multiple buses reject duplicate names (explicit or defaulted)."""
        from zelos_extension_can.cli.app import _create_codecs

        # Explicit duplicate names
        config_dupes = {
            "buses": [
                {
                    "name": "bus",
                    "interface": "virtual",
                    "channel": "vcan0",
                    "database_file": test_dbc_path,
                },
                {
                    "name": "bus",
                    "interface": "virtual",
                    "channel": "vcan1",
                    "database_file": test_dbc_path,
                },
            ]
        }
        with patch("zelos_sdk.TraceSource"), pytest.raises(SystemExit):
            _create_codecs(config_dupes, Path(test_dbc_path))

        # Same channel = same default name = collision
        config_same_channel = {
            "buses": [
                {"interface": "virtual", "channel": "vcan0", "database_file": test_dbc_path},
                {"interface": "virtual", "channel": "vcan0", "database_file": test_dbc_path},
            ]
        }
        with patch("zelos_sdk.TraceSource"), pytest.raises(SystemExit):
            _create_codecs(config_same_channel, Path(test_dbc_path))
