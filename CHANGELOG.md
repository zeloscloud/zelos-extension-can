# Changelog

All notable changes to the Zelos CAN extension are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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
