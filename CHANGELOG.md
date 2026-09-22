# Changelog

All notable changes to the Zelos CAN extension are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- **Auto-configure** on the config form: one `zelos-socketcan` bus per SocketCAN
  interface on the machine running the agent, hardware before `vcan`.
- **A picker on a SocketCAN bus's Channel**, listing that machine's CAN
  interfaces with their state and driver (`can0 (up, gs_usb)`). A name typed by
  hand still works.
- **A list of database files per bus**, applied in order: a later file wins a
  message an earlier one defines differently.
- **Definitions of one frame id coexist**: two DBCs naming one id differently
  both decode, each into its own `<id>_<Name>` table, and decoded tables carry
  the `zelos.can.message.v1` event type; only the same id under the same name
  is a conflict.
- **Advanced `prefix`**: the leading trace source every bus publishes under.
  Clear it for one source per bus.
- Raw frames are logged as the typed `Frame` event (the well-known `CanFrame`
  schema) instead of an ad-hoc one.
- **Hardware timestamps on `ssh-socketcan`** (new per-bus setting, default on):
  `candump -H` where the edge supports it, wall clock for an interface with no
  hardware clock.
- DBC provenance on the wire: `dbcs`, `dbc_conflicts`, `dbc_overlaps`, and a
  per-message `database` on `get_tx_state` / `list_messages` /
  `describe_message`.
- **Every definition is addressable by `key`** (`0334_Merge_Moved`, its trace
  event name): `list_messages` lists them all, the transmit actions take a key
  or a name only one definition carries, and a name at several ids refuses to
  transmit rather than picking one.

### Changed
- Default trace paths are `CAN/<bus>/...` — decoded signals, raw frames, and
  the extension's own logs all under one source.
- The separate `<bus>_raw` source is gone; raw frames ride the bus's source.
- Raw frame logging now defaults to on.
- `log_level` and the four per-bus settings (raw frames, receive own messages,
  emit schemas on init, timestamp mode) moved into Advanced, one value for
  every bus. A legacy per-bus value still wins.
- An unnamed bus is named after its channel, sanitized for trace names.
- The config form reads in the order you fill it in: what identifies the edge
  (remote host, SSH user) or the channel comes before the label and the DBCs.
- A bus's `name` is titled **Channel Display Name**, next to **Channel**: it is
  the label the channel appears under in the trace, not a second identity.
- `zelos-sdk>=0.0.12a1`, the first release whose events carry their type through
  the trace stack, which is what a `zelos.can.frame.v1` drop routes on, and
  `zelos-can>=0.0.9`, which merges several DBCs, lets two definitions of one id
  coexist, and types every signal the way the cantools path does.

### Fixed
- The legacy singular `database_file` left a bare label under the DBC list in
  the app's config form. It is no longer declared; a config carrying one is
  still read and folded into the list.
- `convert` takes `--prefix` so a conversion is named like a live bus.
- On `ssh-socketcan`, a CAN interface the edge does not have now stops a bus
  that has never connected (wrong `remote_channel`, reported with the fix)
  instead of retrying forever. After one working session the same failure is
  transient again — a rebooting edge or a re-enumerating adapter reconnects.

### Fixed
- **DBC files saved in cp1252/Latin-1 now load.** A DBC written by Windows CAN
  tooling — e.g. a `"°C"` unit stored as the single byte `0xB0` — failed codec
  startup with `Failed to load DBC database from file`. Requires
  `zelos-can>=0.0.7`.

### Changed
- DBC load failures now report the file path and underlying cause instead of a
  bare one-line error, and malformed DBCs report the offending line/column.
- Python log lines now carry UTC ISO 8601 timestamps with milliseconds, matching
  the SDK's Rust tracing format in the same extension log stream.

## [0.1.13]

### Added
- **`ssh-socketcan` interface** — trace a remote edge device's SocketCAN bus
  over SSH using the edge's own `can-utils` (`candump`/`cansend`). Nothing is
  deployed on the edge and no local `vcan` is required, so it runs from macOS,
  Linux, or Windows. Decode, tracing, metrics, and periodic transmit run in the
  Rust `zelos-can` pipeline, identical to the native `zelos-socketcan` path.
  Configure with `remote_host`, `remote_channel`, `ssh_user`, `ssh_port`,
  `ssh_key_path`, and `ssh_extra_opts`. See the README for setup and the SSH
  prerequisites (key auth + a trusted host key).

### Changed
- Connection failures on `ssh-socketcan` now fail fast with a clear, actionable
  message (host-key not trusted, authentication, unreachable host, missing
  remote `can-utils`) instead of retrying a doomed connection silently.

### Requirements
- Requires `zelos-can >= 0.0.7a1` (adds the `ExternalBus` port the
  `ssh-socketcan` transport feeds).
