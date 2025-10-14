# Changelog

All notable changes to Zelos Extension Can are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial implementation of data streaming
- Configurable interval settings (0.001-1.0 seconds)
- Get Status action for runtime information
- Set Interval action for dynamic configuration
- JSON Schema-based configuration validation
- Comprehensive error handling and logging
- Graceful shutdown on SIGTERM/SIGINT

### Development
- UV-based dependency management
- Ruff linting and formatting
- ty type checking (extremely fast, Rust-based)
- pytest test suite
- Pre-commit hooks for code quality
- GitHub Actions CI/CD workflows
- Automated packaging for marketplace

## [0.1.0] - YYYY-MM-DD

### Added
- Initial release generated from [cookiecutter-zelos-extension](https://github.com/zeloscloud/cookiecutter-zelos-extension)
- Basic extension structure with working example
- Real-time data streaming with zelos-sdk
- Configuration management system
- Interactive actions support
- Production-ready error handling

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
