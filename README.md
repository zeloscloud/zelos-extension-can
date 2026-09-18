# Zelos extension for CAN (Controller Area Network)

## Features

- 📊 **Visualizing CAN frames** - Real-time decoding of CAN messages
- 📤 **Sending CAN frames** - Send CAN messages directly from the Zelos App
- ⚙️ **Supports any CAN HW/SW stack** - Support for SocketCAN, PCAN, Kvaser, Vector, and virtual interfaces
- 📄 **Multiple database formats** - Supports DBC, ARXML, KCD, and SYM formats
- 🗂️ **Several databases per bus** - Layer DBCs in order; a later file wins a conflicting message id
- 📁 **Trace file conversion** - Convert CAN logs to Zelos format for offline analysis
- 🚗 **Demo mode** - Built-in EV simulation for testing without hardware

## Quick Start

1. **Install** the extension from the Zelos App
2. **Configure** your CAN connection and add your database files (.dbc, .arxml, .kcd, or .sym)
3. **Start** the extension to begin streaming data
4. **View** real-time data in your Zelos App

## Configuration

All configuration is managed through the Zelos App settings interface.

### Filling in the form

Both hooks read the machine running the agent, need no privileges, and work
before the extension has ever started. Neither finds anything on macOS/Windows,
which have no SocketCAN.

| Hook | What it does |
|---|---|
| **Auto-configure** (button above the form) | One `zelos-socketcan` bus per SocketCAN interface on that machine, hardware before `vcan`. Review it, save, then start. Advanced settings are left as they are. |
| **Choose** (beside a bus's Channel) | Lists that machine's CAN interfaces, each name beside its detail — `can0` / `up, gs_usb`, `vcan0` / `virtual` — for a `socketcan` / `zelos-socketcan` bus. You can still type a name. |

### Required Settings
- **Interface**: Choose your CAN adapter type (zelos-socketcan, ssh-socketcan, socketcan, pcan, kvaser, vector, virtual, or demo). On Linux, `zelos-socketcan` is the recommended local SocketCAN option — it is backed by the Rust `zelos-can` bus for higher-throughput, drop-resistant capture. To trace a **remote** device's CAN bus over SSH (from any OS), use `ssh-socketcan` — see [Remote CAN over SSH](#remote-can-over-ssh-ssh-socketcan) below.
- **Channel**: Specify the CAN channel/device name

### Per-Bus Settings
| Setting | What it does |
|---|---|
| **Database Files (.dbc)** | Ordered list of CAN databases. Order is precedence: a later file wins a message id an earlier one defines differently. Leave empty for a raw-frames-only bus. |
| **On Conflicting Definitions** | `warn` (default) keeps the later file's definition; `error` refuses to start. |
| **Name** | Trace segment for this bus. Letters, digits, space, `_`, `-` only. Defaults to the sanitized channel. |
| **Bitrate** | CAN bus bitrate (default 500000). |
| **FD Mode** | Enable CAN-FD support. |

### Advanced Settings

One value each, applied to every bus.

| Setting | What it does |
|---|---|
| **Prefix** | Leading trace source every bus publishes under (default `CAN`). |
| **Log Raw CAN Frames** | Log undecoded frames alongside the decoded signals (default on). |
| **Receive Own Messages** | Receive frames this host transmits. |
| **Emit Schemas On Init** | Register every message schema at startup instead of lazily. |
| **Timestamp Mode** | How to interpret the interface's timestamp (auto, absolute, ignore). |
| **Log Level** | Logging verbosity for all buses. |

### Trace layout

With the default prefix, one source carries every bus. Every interface names
events the same way; a conversion uses the input file's stem as its segment.

| What | Prefix `CAN` | Prefix cleared |
|---|---|---|
| Decoded signals | `CAN/can0/0064_DUT_Status` | `can0/0064_DUT_Status` |
| Undecoded frames | `CAN/can0/Frame` | `can0/Frame` |
| Extension logs | `CAN/log` | `can_log/log` |
| Converted `capture.log` | `CAN/capture/0064_DUT_Status` | `capture/0064_DUT_Status` |

Clear **Prefix** in Advanced settings to keep the previous layout: one source
per bus (or per converted file) with events unprefixed.

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
- **Nothing to do about host keys** on the default **SSH Host Key Policy**
  (`auto`) — including after a reimage, which gives the device a new one.

### Settings
- **Remote Host** (required): edge hostname or IP (or an `~/.ssh/config` alias).
- **Remote Channel**: SocketCAN interface on the edge (default `can0`).
- **SSH User**: login user on the edge (optional if set in `~/.ssh/config`).
- **SSH Port**: default `22`.
- **SSH Key Path**: private key to authenticate with (optional).
- **SSH Host Key Policy**: `auto` (default) trusts whatever host key the edge
  presents and records nothing, so a reimaged device reconnects with no manual
  step; `strict` uses your `~/.ssh/known_hosts`, where an unknown or changed key
  stops the bus with the command that fixes it.
- **SSH Extra Options**: extra `ssh` flags, e.g. a `-J bastion` jump host.
  Placed before the options above; `ssh` honours the first `-o` it sees, so
  these win over the settings above.
- Plus the shared **Database Files** setting and the global **Advanced** ones.

### Notes
- A failure nothing but an operator can fix — authentication, an untrusted host
  key under `strict`, missing `can-utils` on the edge, or a **Remote Channel**
  the edge does not have on a bus that has never once connected — is reported
  once with the exact command that fixes it (resolved for this bus and your OS)
  and the bus stops, rather than retrying behind your back.
- Such a permanent failure on ONE ssh bus stops the extension, and so every
  other bus with it — the same as a bus that cannot start at all.
- Transient failures (unreachable, DNS, a dropped link, or a CAN interface that
  went away after a working session — a rebooting edge, a re-enumerating
  adapter) reconnect automatically with backoff; decoded state and any armed
  periodic transmissions are preserved across the reconnect.
- Timestamps in `auto`/`absolute` mode come from the **edge's** clock — keep the
  edge's time in sync (NTP) if absolute timestamps matter.

## Actions

The extension provides several actions accessible from the Zelos App:

- **List Codecs**: Names of every configured bus
- **Get TX State**: One bus's periodics, DBC list, and bus-health metrics
- **List Messages** / **Describe Message**: Browse the merged DBC message set
- **Send Raw** / **Send Message** / **Encode Preview**: Transmit or preview one frame
- **Start Periodic Raw** / **Start Periodic Message** / **Stop Periodic**: Armed periodic transmit
- **Convert Trace File** / **Convert CAN Log**: Convert a CAN log to a Zelos trace (`.trz`)
- **Export Trace to Log**: Export raw frames from a `.trz` back to candump format

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
# Launch trace process (pass several DBCs to layer them, later files win)
uv run main.py trace socketcan can0 /path/to/file.dbc

# Name the trace source after the bus instead of the prefix
uv run main.py trace socketcan can0 /path/to/file.dbc --prefix ''

# Launch trace process and record to .trz file
uv run main.py trace socketcan can0 /path/to/file.dbc --file

# Convert candump log to Zelos trace format (supports .asc, .blf, .trc, .log, .csv, .mf4)
uv run main.py convert capture.log vehicle.dbc

# Convert against several DBCs, or none at all (raw frames only)
uv run main.py convert capture.log base.dbc overlay.dbc
uv run main.py convert capture.log

# Name the trace source after the input file instead of the prefix
uv run main.py convert capture.log vehicle.dbc --prefix ''
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
