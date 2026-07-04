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
- **Interface**: Choose your CAN adapter type (zelos-socketcan, ssh-socketcan, socketcan, pcan, kvaser, vector, virtual, or demo). On Linux, `zelos-socketcan` is the recommended local SocketCAN option — it is backed by the Rust `zelos-can` bus for higher-throughput, drop-resistant capture. To trace a **remote** device's CAN bus over SSH (from any OS), use `ssh-socketcan` — see [Remote CAN over SSH](#remote-can-over-ssh-ssh-socketcan) below.
- **Channel**: Specify the CAN channel/device name

### Optional Settings
- **Bitrate**: CAN bus bitrate (default: 500000)
- **FD Mode**: Enable CAN-FD support
- **Timestamp Mode**: Control how timestamps are interpreted (auto, absolute, ignore)
- **Schema Emission**: Emit all schemas on startup or lazily as messages appear
- **Raw Frame Logging**: Log undecoded raw CAN frames for debugging

## Remote CAN over SSH (`ssh-socketcan`)

Trace a remote edge device's SocketCAN bus over an SSH connection, using the
edge's **own** `can-utils`. Nothing is installed on the edge, no local `vcan` is
needed, and it runs from macOS, Linux, or Windows. Decode, tracing, metrics, and
periodic transmit all run in the same high-throughput Rust pipeline as the local
`zelos-socketcan` interface. Sent frames are echoed back by the edge's kernel
loopback, so every transmit is traced exactly once.

### Prerequisites on the edge
- An SSH server reachable from the machine running Zelos.
- `can-utils` installed (`candump` and `cansend` on `PATH`).
- A SocketCAN interface that is up (e.g. `can0`).

### Prerequisites on this machine
- An `ssh` client on `PATH`.
- **Key-based SSH auth** to the edge. The extension connects non-interactively
  (`BatchMode`), so password prompts are not possible — set up a key
  (`ssh-copy-id user@host`) or point **SSH Key Path** at your private key.
- **A trusted host key.** On the *first* connection to a new edge, SSH must
  already know its host key or the connection fails with
  `Host key verification failed`. Resolve it once, up front, by either:
  - connecting manually a single time to accept the key: `ssh user@host`, or
  - adding it non-interactively: `ssh-keyscan -H host >> ~/.ssh/known_hosts`, or
  - adding `-o StrictHostKeyChecking=accept-new` to **SSH Extra Options**
    (trust-on-first-use; still detects a later key change).

### Settings
- **Remote Host** (required): edge hostname or IP (or an `~/.ssh/config` alias).
- **Remote Channel**: SocketCAN interface on the edge (default `can0`).
- **SSH User**: login user on the edge (optional if set in `~/.ssh/config`).
- **SSH Port**: default `22`.
- **SSH Key Path**: private key to authenticate with (optional).
- **SSH Extra Options**: extra `ssh` flags, e.g.
  `-o StrictHostKeyChecking=accept-new` or a `-J bastion` jump host.
- Plus the shared **Database File**, **Timestamp Mode**, **Raw Frame Logging**,
  and **Schema Emission** settings.

### Notes
- If a connection fails, the extension reports a specific reason (untrusted host
  key, authentication, unreachable host, or missing `can-utils` on the edge)
  rather than retrying silently.
- If the link drops mid-capture, it reconnects automatically; decoded state and
  any armed periodic transmissions are preserved across the reconnect.
- Timestamps in `auto`/`absolute` mode come from the **edge's** clock — keep the
  edge's time in sync (NTP) if absolute timestamps matter.

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
