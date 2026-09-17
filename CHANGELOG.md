# Changelog

All notable changes to the Zelos CAN extension are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- **A list of database files per bus**, applied in order: a later file wins a
  message id an earlier one defines differently.
- **`dbc_conflict`** per bus: `warn` keeps the later definition, `error` refuses
  to start.
- **Advanced `prefix`**: the leading trace source every bus publishes under.
  Clear it for one source per bus.
- Raw frames are logged as the typed `Frame` event (the well-known `CanFrame`
  schema) instead of an ad-hoc one.
- DBC provenance on the wire: `dbcs`, `dbc_conflicts`, and a per-message
  `database` on `get_tx_state` / `list_messages` / `describe_message`.

### Changed
- Default trace paths are `CAN/<bus>/...` — decoded signals, raw frames, and
  the extension's own logs all under one source.
- The separate `<bus>_raw` source is gone; raw frames ride the bus's source.
- Raw frame logging now defaults to on.
- `log_level` and the four per-bus settings (raw frames, receive own messages,
  emit schemas on init, timestamp mode) moved into Advanced, one value for
  every bus. A legacy per-bus value still wins.
- An unnamed bus is named after its channel, sanitized for trace names.
- `convert` takes `--prefix` so a conversion is named like a live bus.

### Fixed
- **DBC files saved in cp1252/Latin-1 now load.** A DBC written by Windows CAN
  tooling — e.g. a `"°C"` unit stored as the single byte `0xB0` — failed codec
  startup with `Failed to load DBC database from file`. Requires
  `zelos-can>=0.0.7`.

### Changed
- DBC load failures now report the file path and underlying cause instead of a
  bare one-line error, and malformed DBCs report the offending line/column.

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
