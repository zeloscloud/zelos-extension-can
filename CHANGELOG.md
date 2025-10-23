# Changelog

All notable changes to Zelos Extension Can are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Smart timestamp handling** with auto-detection for boot-relative timestamps
  - Three modes: `auto` (detects and converts boot-relative), `absolute` (uses as-is), `ignore` (system time)
  - Automatically detects timestamps < 1 hour as boot-relative and applies offset
  - Preserves relative timing between messages with microsecond precision
- **Built-in demo mode** with physics-based EV simulator
  - Realistic battery, motor, and vehicle dynamics
  - Automatic CAN message generation at realistic rates
  - Bundled demo.dbc with 12 EV-related messages
  - Accessible via `just demo` or `python main.py --demo`
- **CLI argument support** with `--demo` flag for easy demo mode activation
- **Comprehensive test suite** with 42 tests including 9 timestamp-specific tests and 3 config_json tests
- **Configuration validation** for all config fields including timestamp_mode and config_json
- **Modular demo structure** in `zelos_extension_can/demo/` for maintainability
- **Advanced configuration via config_json** - Pass interface-specific python-can kwargs as JSON
  - Allows custom options like `app_name`, `rx_queue_size`, `timing`, etc.
  - Merged with form-based config for maximum flexibility
  - Full validation with helpful error messages

### Changed
- Reorganized demo code into `zelos_extension_can/demo/` module
- Moved test.dbc to `tests/files/` for better organization
- Updated config schema UI order (timestamp_mode and demo_mode at end)
- Enhanced README with updated demo instructions and configuration table
- Improved project structure documentation

### Fixed
- Timestamp handling now correctly converts boot-relative timestamps to wall-clock time
- Demo mode no longer manipulates config.json file
- Configuration validation now properly validates timestamp_mode enum values

### Development
- UV-based dependency management
- Ruff linting and formatting
- Justfile commands for common tasks (install, test, demo, release)

## [0.1.0] - YYYY-MM-DD

### Added
- Initial CAN bus monitoring with python-can
- DBC file decoding with cantools
- Dynamic trace event generation from DBC
- Multiplexed CAN message support
- Periodic message transmission via actions
- Virtual bus support for development
- Interactive actions (Get Status, Send Message, Start/Stop Periodic)
- Platform-specific defaults (Linux, macOS, Windows)
- Data-URL support for DBC file uploads
- Comprehensive error handling and logging
- Graceful shutdown on SIGTERM/SIGINT

---

## How to Update This Changelog

When making changes, add entries under `[Unreleased]` in the appropriate category:

- **Added**: New features
- **Changed**: Changes to existing functionality
- **Deprecated**: Soon-to-be removed features
- **Removed**: Removed features
- **Fixed**: Bug fixes
- **Security**: Security fixes

Before releasing, move `[Unreleased]` items to a new version section with the date:

```markdown
## [1.0.0] - 2024-12-15

### Added
- Amazing new feature

## [0.1.0] - 2024-12-01
...
```
