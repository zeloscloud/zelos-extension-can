# Zelos extension for CAN (Controller Area Network)

## Features

- 📊 **Visualizing CAN frames** - Real-time decoding of CAN messages
- 📤 **Sending CAN frames** - Send CAN messages directly from the Zelos App
- ⚙️ **Supports any CAN HW/SW stack** - Support for SocketCAN, PCAN, Kvaser, Vector, and virtual interfaces
- 📄 **Multiple database formats** - Supports DBC, ARXML, KCD, and SYM formats
- 📁 **Trace file conversion** - Convert CAN logs to Zelos format for offline analysis
- 🚗 **Demo mode** - Built-in EV simulation for testing without hardware

## Quick Start

1. **Install** the extension from the Zelos App
2. **Configure** your CAN connection and provide your database file (.dbc, .arxml, .kcd, or .sym)
3. **Start** the extension to begin streaming data
4. **View** real-time data in your Zelos App

## Configuration

All configuration is managed through the Zelos App settings interface.

### Required Settings
- **Database File**: Upload your CAN database file (`.dbc`, `.arxml`, `.kcd`, or `.sym` format)
- **Interface**: Choose your CAN adapter type (socketcan, pcan, kvaser, vector, virtual, or demo)
- **Channel**: Specify the CAN channel/device name

### Optional Settings
- **Bitrate**: CAN bus bitrate (default: 500000)
- **FD Mode**: Enable CAN-FD support
- **Timestamp Mode**: Control how timestamps are interpreted (auto, absolute, ignore)
- **Schema Emission**: Emit all schemas on startup or lazily as messages appear
- **Raw Frame Logging**: Log undecoded raw CAN frames for debugging

## Actions

The extension provides several actions accessible from the Zelos App:

- **Get Status**: View current CAN bus connection status and configuration
- **Send Message**: Send a single CAN message with custom signal values
- **Start Periodic Message**: Begin periodic transmission of a CAN message at a specified interval
- **Stop Periodic Message**: Stop an active periodic transmission
- **List Periodic Tasks**: View all currently running periodic transmissions
- **Get Metrics**: View performance statistics (message counts, rates, errors)
- **List Messages**: Browse all CAN messages defined in your DBC file
- **Convert Trace File**: Convert CAN log files to Zelos trace format for offline analysis

## What is CAN?
[See this tutorial](https://www.csselectronics.com/pages/can-bus-simple-intro-tutorial)

## Development

Want to contribute or modify this extension? See [CONTRIBUTING.md](CONTRIBUTING.md) for the complete developer guide.

## Links

- **Repository**: [github.com/zeloscloud/zelos-extension-can](https://github.com/zeloscloud/zelos-extension-can)
- **Issues**: [Report bugs or request features](https://github.com/zeloscloud/zelos-extension-can/issues)

## CLI Usage

The extension includes a command-line interface for advanced use cases. No installation required - just use `uv run`:

> **Tip:** Run `pip install .` to install and use `zelos-extension-can <args>` from anywhere.

### CAN Bus Tracing

```bash
# Launch trace process
uv run main.py trace socketcan can0 /path/to/file.dbc

# Launch trace process and record to .trz file
uv run main.py trace socketcan can0 /path/to/file.dbc --file

# Convert candump log to Zelos trace format (supports .asc, .blf, .trc, .log, .csv, .mf4)
uv run main.py convert capture.log vehicle.dbc
```

## Support

For help and support:
- 📖 [Zelos Documentation](https://docs.zeloscloud.io)
- 🐛 [GitHub Issues](https://github.com/zeloscloud/zelos-extension-can/issues)
- 📧 help@zeloscloud.io

## License

MIT License - see [LICENSE](LICENSE) for details.

---

**Built with [Zelos](https://zeloscloud.io)**
