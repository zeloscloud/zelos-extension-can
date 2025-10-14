"""Essential unit tests for CAN codec."""

from unittest.mock import patch

import pytest

from zelos_extension_can.can_codec import CanCodec
from zelos_extension_can.schema_utils import cantools_signal_to_trace_type


@pytest.fixture
def test_dbc_path():
    """Path to test DBC file."""
    return "/Users/tkeairns/zelos/src/api/py/test/data/test.dbc"


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
        assert len(codec.db.messages) == 9  # test.dbc has 9 messages

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
        """Test event names follow {id:04x}_{name} format."""
        for msg in codec.db.messages:
            event_name = codec._get_event_name(msg)
            assert "_" in event_name
            msg_id_hex, msg_name = event_name.split("_", 1)
            assert len(msg_id_hex) == 4
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


class TestActions:
    """Test action methods."""

    def test_get_status(self, codec):
        """Test get_status action returns expected fields."""
        status = codec.get_status()
        assert "running" in status
        assert "interface" in status
        assert "channel" in status
        assert "messages_in_dbc" in status
        assert status["messages_in_dbc"] == 9
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
        assert result["count"] == 9
        assert len(result["messages"]) == 9

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

    def test_validate_invalid_bitrate(self):
        """Test validation catches invalid bitrate."""
        from zelos_extension_can.utils.config import validate_config

        config = {
            "interface": "virtual",
            "channel": "vcan0",
            "dbc_file": "/Users/tkeairns/zelos/src/api/py/test/data/test.dbc",
            "bitrate": 999999,
        }
        errors = validate_config(config)
        assert any("Invalid bitrate" in e for e in errors)

    def test_validate_socketcan_channel(self):
        """Test validation of socketcan channel naming."""
        from zelos_extension_can.utils.config import validate_config

        config = {
            "interface": "socketcan",
            "channel": "invalid_name",
            "dbc_file": "/Users/tkeairns/zelos/src/api/py/test/data/test.dbc",
        }
        errors = validate_config(config)
        assert any("socketcan interface requires" in e for e in errors)
