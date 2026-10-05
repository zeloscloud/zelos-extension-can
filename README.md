# Zelos extension for CAN (Controller Area Network)

## Features

- 📊 **Visualizing CAN frames** - Real-time decoding of CAN messages
- 📤 **Sending CAN frames** - Send CAN messages directly from the Zelos App
- ⚙️ **Supports any CAN HW/SW stack** - Support for SocketCAN, PCAN, Kvaser, Vector, and virtual interfaces
- 📄 **Multiple database formats** - Supports DBC, ARXML, KCD, and SYM formats
- 🗂️ **Several databases per bus** - Layer DBCs in order; definitions of one message id coexist
- 📁 **Trace file conversion** - Convert CAN logs to Zelos format for offline analysis
- 🚗 **Demo mode** - Built-in EV simulation for testing without hardware

## Quick Start

[![Install the CAN extension, auto-configure can0, start it and view frames](assets/howto/quickstart/quickstart.avif)](assets/howto/quickstart/quickstart.mp4)

From the CLI, on the agent that has the CAN interface:

```bash
zelos extensions install zeloscloud/zelos-extension-can
zelos extensions start zeloscloud.zelos-extension-can \
  --config '{"buses": [{"interface": "SocketCAN", "channel": "can0"}]}'
```

In the app:

1. **Install** the extension from the Zelos App
2. **Configure** your CAN connection and add your database files (.dbc, .arxml, .kcd, or .sym)
3. **Start** the extension to begin streaming data
4. **View** real-time data in your Zelos App

## Configuration

All configuration is managed through the Zelos App settings interface.

### Filling in the form

Both hooks read the machine running the agent, need no privileges, and work
before the extension has ever started.

| Hook | What it does |
|---|---|
| **Auto-configure** (button above the form) | One bus per CAN adapter on that machine. On Linux, one SocketCAN bus per SocketCAN interface, hardware before `vcan`, a down interface included as it is. On macOS/Windows, one PCAN, Kvaser or Vector bus per channel whose vendor driver finds it. On any OS, one slcan bus per CANable or CANUSB serial port. Adapter buses start at 500 kbit/s, so set Bitrate to match your bus. With no adapter, one Demo bus. Review it, save, then start. Advanced settings are left as they are. |
| **Choose** (beside a bus's Channel) | Lists that machine's CAN interfaces, each name beside its detail (`can0` / `up, gs_usb`, `vcan0` / `virtual`), for a SocketCAN bus. You can still type a name. |

### Required Settings
- **Interface**: the CAN adapter. The config stores the label; the extension opens its python-can interface.

  | Interface | python-can | What |
  |---|---|---|
  | SocketCAN | `zelos-socketcan` | Local SocketCAN on zelos-can's Rust bus, Linux. The recommended local option |
  | SocketCAN over SSH | `zelos-ssh-socketcan` | A remote device's SocketCAN over SSH, any OS; see [Remote CAN over SSH](#remote-can-over-ssh) |
  | PCAN, Kvaser, Vector | `pcan`, `kvaser`, `vector` | Vendor adapters |
  | slcan (serial) | `slcan` | CANable, CANUSB and other LAWICEL serial adapters |
  | Other (python-can) | from Advanced Configuration (JSON) | Any python-can interface |
  | Demo | | Built-in EV simulator |

- **Channel**: Specify the CAN channel/device name. For slcan (serial) (CANable, CANUSB and other LAWICEL serial adapters), this is the serial port: `/dev/ttyACM0`, `/dev/tty.usbmodem...`, `COM3`.

### Per-Bus Settings
| Setting | What it does |
|---|---|
| **Database Files (.dbc)** | Ordered list of CAN databases. Order is precedence: a later file wins a message an earlier one defines differently. Leave empty for a raw-frames-only bus. |
| **Name** | Trace segment for this bus. Letters, digits, space, `_`, `-` only. Defaults to the sanitized channel. |
| **Bitrate** | CAN bus bitrate (default 500000). |
| **FD Mode** | Enable CAN-FD support. |
| **CANopen nodes** | CANopen devices on the bus: node ID, EDS/DCF, name. See [CANopen](#canopen). |

Two databases that define one message id:

| Case | What happens |
|---|---|
| **Overlap** — same id, **different** names | Both definitions survive: a frame decodes under each, into its own table (`0302_Batt_Status` *and* `0302_Batt_Debug`). Logged at INFO, reported as `dbc_overlaps`. |
| **Conflict** — same id, **same** name, different layout | The later file's definition wins, the other is dropped with a warning, and the pair is listed under `dbc_conflicts` on `get_tx_state`. |

One message name at two ids (a moved message) is neither: both definitions
survive and both are listed. Address each by its `key` — a transmit by the bare
name refuses and names the keys.

### J1939

On a SocketCAN or SocketCAN over SSH bus, and in `convert`, the messages the
DBC marks J1939 (`VFrameFormat` = `J1939PG` per message, or `ProtocolType` = `J1939` on
the database, which SAE, CSS and Vector J1939
DBCs do) are J1939 parameter groups. **Advanced > J1939** (off by default)
makes every 29-bit message one, for a DBC without that attribute. `convert`
and `trace` have `--j1939` for the same.

| What | Behavior |
|---|---|
| **Matching** | By PGN, whatever the priority. A message the DBC defines at source address `0xFE` decodes from any sender. |
| **Tables** | One per sender: `0cf00400_EEC1`, `0cf00401_EEC1`. |
| **One message, several definitions** | A sender decodes under its own definition, whatever the destination. A sender the DBC does not name decodes under the first definition when they all share one layout, and is left undecoded when layouts differ. |
| **Messages over 8 bytes** | Transport protocol transfers (BAM, RTS/CTS) are rebuilt and decoded, stamped with their last packet. |
| **Not available, error** | A raw value J1939 reserves for these (above `0xFA` at 8 bits, `0xFAFF` at 16, and so on) decodes to empty. It stays a value when the DBC's range for the signal takes it in, or a `VAL_` labels it. |
| **Address claim** | Off until **Advanced > J1939 Node** names a DBC node (see [J1939 node identity](#j1939-node-identity)), or the `J1939 Claim` action runs. The node defends its address against a higher NAME and yields to a lower one; NAME bit 63 (arbitrary address capable) lets it move to a free address in 128..247. Claimed again when the bus reopens. `get_tx_state` shows `j1939.address` (empty while claiming, or lost). |
| **Transmit** | From the claimed address only. Up to 8 bytes is one frame; longer goes by the transport protocol, BAM to everyone or RTS/CTS to one node. A send waits up to 5 s and returns `completed` / `error`. |
| **J1939-22 (CAN FD)** | `fd` on a send, with CAN-FD Mode on: a Multi-PG frame up to 60 bytes, FD.TP beyond, every frame with BRS. The claim stays on classic frames. |
| **Periodic** | A period whose previous transfer is still running, or whose claim is still settling, is skipped and counted (`skipped` on the periodic). A send that fails, busy or aborted included, counts in `failed` / `last_error` on the periodic and in `tx_errors`. |
| **Counters** | `j1939_tx_completed`, `j1939_tx_aborted`, `j1939_claim_lost`, and `fdtp_*`, `j1939_22_*` on receive. |
| **python-can buses** | No node: the J1939 transmit actions refuse; Encode Preview still works. |

### J1939 node identity

**Advanced > J1939 Node** names the DBC node (`BU_`) every SocketCAN and SocketCAN
over SSH bus acts as. Each such bus claims it when it starts and again when it
reopens. The node's identity comes from its J1939 node attributes, as other J1939 tools
read them:

| Attribute | Identity |
|---|---|
| `NmStationAddress` | Address claimed, 0..253. The default 254 (no address) refuses. |
| `NmJ1939AAC`, `NmJ1939IndustryGroup`, `NmJ1939System`, `NmJ1939SystemInstance`, `NmJ1939Function`, `NmJ1939FunctionInstance`, `NmJ1939ECUInstance`, `NmJ1939ManufacturerCode`, `NmJ1939IdentityNumber` | The 64-bit NAME. Unset ones take the DBC's default. |

The node must be in each such bus's DBCs. A bad identity (unknown node, no address, a field
out of range) fails the start with the reason. Empty (the default): no claim until the
`J1939 Claim` action, and J1939 sends refuse. The J1939 address registry reserves source
address 249 for Function 129 (off-board diagnostic-service tool) in Industry Group 0,
instances 0.

### CANopen

On a SocketCAN or SocketCAN over SSH bus, the bus's **CANopen nodes** decode
as CANopen (CiA 301). **Advanced > CANopen** (off by default) decodes the
protocol messages of every node-id, for a bus that lists none. The CLI `trace`
and `convert` have `--canopen` and a repeatable `--canopen-node ID[:FILE[:NAME]]`
(`--canopen-node 0x20:pdu.eds:pdu`, `--canopen-node 5::left`); the in-app
Convert actions have only the **CANopen** toggle, every node-id without files.
Other interfaces log a warning and decode nothing.

| What | Behavior |
|---|---|
| **Decoded** | NMT, heartbeat and node guarding, EMCY, SYNC, TIME, LSS and SDO transfers, by the predefined connection set. With an EDS or DCF, the node's PDOs too, under its object names. |
| **Node** | **Node ID** 1-127, empty for a DCF that sets `NodeID`. **EDS or DCF file** optional. **Name** names the node's events (default `node<id hex>`); same characters as a bus name. A DBC message on a configured node's COB-ID or PDO is an error, except on its SYNC or TIME COB-ID: there the DBC wins, with a warning. |
| **Status** | `CANopen Nodes`: per node, `configured`, NMT `state`, `last_heartbeat_ns`, `heartbeat_period_ms`, `heartbeat_lost`, `emcy_count`, `last_emcy_code`, `sdo_count`. `Get TX State` carries the `canopen_*` counters: frames, SDO completed / aborted / resync / CRC error, short frames, heartbeat late / lost, unknown nodes, PDO short / remapped / reverted / unlearned, client SDO / timeout / stale. |
| **Transmit** | `CANopen SDO Read` / `SDO Write` / `NMT` [actions](#actions), on a running bus with CANopen decode on (nodes listed or the Advanced toggle): the SDO client matches answers through the decoder. The extension is the SDO client and NMT master, never a node. SDO Write and NMT change device state with no confirmation, like `Send Raw`. |
| **Demo** | **Simulated CANopen node** on a Demo bus adds node `0x20`, a power distribution unit described by the bundled `pdu.eds`, and decodes the demo bus on the Rust path. |

### Advanced Settings

One value each, applied to every bus.

| Setting | What it does |
|---|---|
| **Prefix** | Leading trace source every bus publishes under (default `CAN`). |
| **Log Raw CAN Frames** | Log undecoded frames alongside the decoded signals (default on). |
| **Receive Own Messages** | Receive frames this host transmits. |
| **Emit Schemas On Init** | Register every message schema at startup instead of lazily. |
| **Timestamp Mode** | How to interpret the interface's timestamp (auto, absolute, ignore). |
| **J1939** | Every 29-bit message on a SocketCAN / SocketCAN over SSH bus is a J1939 parameter group, not only those the DBC marks (default off). See [J1939](#j1939). |
| **J1939 Node** | The DBC node the SocketCAN / SocketCAN over SSH buses claim and send as on J1939 (default empty: none). See [J1939 node identity](#j1939-node-identity). |
| **CANopen** | Decode CANopen for every node-id on a SocketCAN / SocketCAN over SSH bus with no CANopen nodes listed (default off). See [CANopen](#canopen). |
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

## Remote CAN over SSH

Trace a remote edge device's SocketCAN bus over an SSH connection, using the
edge's **own** `can-utils`. Nothing is installed on the edge, no local `vcan` is
needed, and it runs from macOS, Linux, or Windows. Decode, tracing, metrics, and
periodic transmit all run in the same high-throughput Rust pipeline as the local
SocketCAN interface. Sent frames are echoed back by the edge's kernel
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
- **Hardware timestamps** (default on): uses the adapter's hardware clock where
  the edge's `candump` supports `-H`. An interface without one (`vcan`, some
  `slcan`) falls back to this machine's wall clock; turn it off to use the edge
  kernel's receive time.
- Plus the shared **Database Files** setting and the global **Advanced** ones.

### Notes
- A failure nothing but an operator can fix — authentication, an untrusted host
  key under `strict`, missing `can-utils` on the edge, or a **Remote Channel**
  the edge does not have on a bus that has never once streamed a frame — is reported
  once with the exact command that fixes it (resolved for this bus and your OS)
  and the bus stops, rather than retrying behind your back.
- Such a permanent failure on ONE ssh bus stops the extension, and so every
  other bus with it — the same as a bus that cannot start at all.
- Transient failures (unreachable, DNS, a dropped link, or a CAN interface that
  went away after a working session — a rebooting edge, a re-enumerating
  adapter) reconnect automatically with backoff; decoded state and any armed
  periodic transmissions are preserved across the reconnect.
- Timestamps in `auto`/`absolute` mode come from the **edge's** clock (its
  adapter's, with **Hardware timestamps** on) — keep the edge's time in sync
  (NTP) if absolute timestamps matter.

## Actions

The extension provides several actions accessible from the Zelos App:

- **List Codecs**: Names of every configured bus
- **Get TX State**: One bus's periodics, DBC list, metrics, and health. `status` is `active`, `warning` (errors on the wire, error counters high, or frames lost on receive), `error` (error-passive, bus-off, adapter unreachable or down, or sends failing), `stopped`, or `unknown`; `health` gives the controller's state, why, and its error counters where the adapter reports them
- **List Messages** / **Describe Message**: Browse the merged DBC message set — one entry per definition, each with a `key` (`0334_Merge_Moved`: its id and name, and the name of its trace event). The transmit actions take a key, or a name only one definition carries.
- **Send Raw** / **Send Message** / **Encode Preview**: Transmit or preview one frame
- **Start Periodic Raw** / **Start Periodic Message** / **Stop Periodic**: Armed periodic transmit
- **J1939 Claim**: Claim an address for a NAME on a SocketCAN / SocketCAN over SSH bus; the J1939 sends go from it
- **J1939 Send** / **J1939 Encode Preview** / **J1939 Start Periodic**: A DBC-encoded parameter group; destination (PDU1) and priority default to the DBC's, `fd` sends J1939-22
- **J1939 Send Raw**: A PGN and its bytes, any length
- **J1939 Request**: Ask a node, or everyone, for a PGN (Request, `0xEA00`); the answer decodes like any other frame
- **CANopen Nodes** / **CANopen Describe**: A bus's CANopen nodes and their state; a configured node's object dictionary entry, or its PDOs, from its EDS/DCF
- **CANopen SDO Read** / **CANopen SDO Write**: Read or write one object on a node (index in hex, subindex). A write is typed by the node's EDS/DCF entry (a chosen data type must agree); without one the data type is required, `bytes` for raw hex. An abort or no answer is an error
- **CANopen NMT**: start, stop, pre_operational, reset_node or reset_communication, to one node or all (node 0, entered explicitly)
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

`trace` takes a python-can interface name, not a form label: `zelos-socketcan` is the Rust bus, and `socketcan` here now means python-can's bus.

```bash
# Launch trace process (pass several DBCs to layer them, later files win)
uv run main.py trace zelos-socketcan can0 /path/to/file.dbc

# Name the trace source after the bus instead of the prefix
uv run main.py trace zelos-socketcan can0 /path/to/file.dbc --prefix ''

# Launch trace process and record to .trz file
uv run main.py trace zelos-socketcan can0 /path/to/file.dbc --file

# Convert a CAN log to Zelos trace format (.asc, .blf, .trc, .log, .csv, .mf4; MF4 needs no asammdf)
# A .csv needs python-can's header: timestamp,arbitration_id,extended,remote,error,dlc,data
uv run main.py convert capture.log vehicle.dbc

# Convert against several DBCs, or none at all (raw frames only)
uv run main.py convert capture.log base.dbc overlay.dbc
uv run main.py convert capture.log

# Name the trace source after the input file instead of the prefix
uv run main.py convert capture.log vehicle.dbc --prefix ''

# Every 29-bit message is a J1939 parameter group, for a DBC that does not mark them
uv run main.py convert capture.log vehicle.dbc --j1939

# CANopen node 0x20 with its EDS, and node 5 without one, named left
uv run main.py convert capture.log --canopen-node 0x20:pdu.eds --canopen-node 5::left
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
