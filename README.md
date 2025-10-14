# CAN Extension for Zelos App

> Monitor and decode CAN bus messages with DBC support

A production-ready Zelos extension for real-time CAN bus monitoring, decoding, and message transmission using python-can and cantools.

## Features

- 🚗 **CAN bus monitoring** - Async reception with python-can (socketcan, PCAN, Kvaser, Vector)
- 📋 **DBC decoding** - Automatic signal extraction and type mapping
- 🔄 **Dynamic schema generation** - Trace events created from DBC at runtime
- 📡 **Multiplexed messages** - Full support for muxed CAN signals
- ⚡ **Periodic transmission** - Send messages at specified rates via actions
- 🧪 **Virtual bus support** - Cross-platform development and testing

## Quick Start

### Production Use

1. **Install** the extension in Zelos App
2. **Configure** your CAN interface and DBC file:
   ```json
   {
     "interface": "socketcan",
     "channel": "can0",
     "bitrate": 500000,
     "dbc_file": "/path/to/your.dbc"
   }
   ```
3. **Start** the extension to begin decoding messages
4. **View** real-time CAN signals in your Zelos dashboard

### Demo Mode (Development)

Run continuous CAN traffic replay with test.dbc:

```bash
cd zelos-extension-can
just install          # Install dependencies
uv run python demo.py # Start demo with 10Hz replay
```

Press Ctrl+C to stop.

## Configuration

| Setting | Type | Options | Description |
|---------|------|---------|-------------|
| **interface** | String | socketcan, virtual, pcan, kvaser, vector | CAN interface type |
| **channel** | String | can0, vcan0, PCAN_USBBUS1, etc. | Channel identifier |
| **bitrate** | Integer | 125000, 250000, 500000, 1000000 | Bus bitrate (bps) |
| **dbc_file** | String | /path/to/file.dbc | DBC database file path |

## Actions

### Get Status
View current bus status and message counts.

### Send Message
Send a single CAN message:
- **msg_id**: Message ID (0-0x7FF)
- **data**: Hex data string (e.g., "01 02 03 04")

### Start Periodic Message
Begin periodic transmission:
- **msg_id**: Message ID
- **data**: Hex data
- **period**: Transmission period in seconds (0.001-10.0)

### Stop Periodic Message
Stop periodic transmission by message ID.

### List Messages
Show all messages defined in loaded DBC.

## Development

```bash
# Install with dev dependencies
just install

# Run linting and type checks
just check

# Run tests
just test

# Create a release
just release 0.1.0
```

## Project Structure

```
zelos-extension-can/
├── main.py                    # Production entry point
├── demo.py                    # Interactive demo (continuous replay)
├── config.json                # Example configuration
├── extension.toml             # Extension manifest
├── config.schema.json         # Configuration UI schema
├── zelos_extension_can/
│   ├── can_codec.py          # Core CAN codec with async reception
│   ├── schema_utils.py       # DBC→SDK type mapping utilities
│   └── utils/
│       └── config.py         # Configuration loading
└── tests/
    └── test_can_codec.py     # Unit tests
```

## Requirements

- **Zelos** v25.0.20+
- **Python** 3.11+ (managed by Zelos/UV)
- **python-can** 4.4.0+
- **cantools** 39.0.0+

## Links

- **Repository**: [github.com/tkeairns/zelos-extension-can](https://github.com/tkeairns/zelos-extension-can)
- **Zelos Docs**: [docs.zeloscloud.io](https://docs.zeloscloud.io)

## License

MIT License - see [LICENSE](LICENSE) for details.
