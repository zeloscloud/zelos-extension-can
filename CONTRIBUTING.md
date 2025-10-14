# Contributing to Zelos Extension Can

Thank you for contributing! This guide covers the development workflow and project structure.

## Development Setup

### Prerequisites

- Python 3.11+
- [UV](https://github.com/astral-sh/uv) - Fast Python package manager
- [just](https://github.com/casey/just) - Command runner (`brew install just` on macOS)
- Git

### Getting Started

```bash
# Clone and set up
git clone https://github.com/tkeairns/zelos-extension-can.git
cd zelos-extension-can
just install

# Verify setup
just check
just test
```

This installs all dependencies, sets up pre-commit hooks, and verifies everything works.

## Development Workflow

### Available Commands

```bash
just install          # Install dependencies and pre-commit hooks
just dev              # Run extension locally
just format           # Auto-format code with ruff
just check            # Run linting (ruff) and type checking (ty)
just test             # Run test suite with pytest
just package          # Create distribution tarball
just release VERSION  # Create a new release (e.g., just release 1.0.0)
just clean            # Clean build artifacts
```

### Typical Workflow

```bash
# 1. Make changes
vim zelos_extension_can/extension.py

# 2. Format and verify
just format
just check
just test

# 3. Test locally
just dev  # Press Ctrl+C to stop

# 4. Commit (pre-commit hooks run automatically)
git add .
git commit -m "feat: add new feature"
```

## Project Structure

```
zelos-extension-can/
├── extension.toml              # Extension manifest (required by Zelos)
├── config.schema.json          # Configuration UI schema (JSON Schema/RJSF)
├── main.py                     # Entry point - initializes and runs extension
├── pyproject.toml              # Python dependencies and metadata
├── requirements.txt            # Generated for Zelos runtime
├── Justfile                    # Development commands
├── zelos_extension_can/
│   ├── __init__.py
│   ├── extension.py            # Core: SensorMonitor class with actions
│   └── utils/
│       ├── __init__.py
│       └── config.py           # Configuration loading
├── tests/
│   ├── test_config.py
│   └── test_extension.py
├── assets/
│   └── icon.svg                # Marketplace icon
├── scripts/
│   └── package_extension.py    # Packaging for marketplace
└── .github/workflows/
    ├── CI.yml                  # CI on pushes/PRs
    └── release.yml             # Release automation on tags
```

### Key Files

- **`extension.toml`**: Extension metadata, version, and runtime config
- **`main.py`**: Entry point with signal handlers and SDK initialization
- **`zelos_extension_can/extension.py`**: Core monitor class with lifecycle, actions, and data streaming
- **`config.schema.json`**: JSON Schema defining configuration UI in Zelos App
- **`zelos_extension_can/utils/config.py`**: Loads and validates configuration from config.json

## Testing

### Running Tests

```bash
# All tests
just test

# Specific file
uv run pytest tests/test_extension.py

# With coverage
uv run pytest --cov=zelos_extension_can
```

### Writing Tests

```python
# tests/test_feature.py
from zelos_extension_can.extension import SensorMonitor


def test_feature(check):
    config = {"sensor_name": "test", "interval": 0.1}
    monitor = SensorMonitor(config)

    status = monitor.get_status()
    check.that(status["state"], "==", "IDLE")
    check.that(status["running"], "is", False)
```

The Zelos pytest plugins are enabled out of the box.

See the [Zelos testing guide](https://docs.zeloscloud.io/sdk/testing/) for advanced workflows.

### Local Testing

```bash
# Create test config
echo '{"sensor_name": "test-sensor", "interval": 0.1}' > config.json

# Run extension
just dev  # Ctrl+C to stop

# Extension logs to stdout
```

## Code Quality

### Tools

- **[Ruff](https://github.com/astral-sh/ruff)** - Linting and formatting
- **[ty](https://github.com/astral-sh/ty)** - Fast type checker (Rust-based)
- **[pytest](https://docs.pytest.org/)** - Testing framework
- **[pre-commit](https://pre-commit.com/)** - Git hooks

### Standards

- Type hints on all function signatures
- Docstrings for public classes and methods
- 100 character line limit
- PEP 8 naming conventions

### Pre-commit Hooks

Hooks run automatically on commit:
- Ruff linting and formatting
- Type checking with ty
- YAML/TOML validation
- Trailing whitespace removal

Run manually:
```bash
pre-commit run --all-files
```

## Dependencies

```bash
# Add runtime dependency
uv add package-name

# Add dev dependency
uv add --dev package-name

# Update requirements.txt (for Zelos runtime)
uv pip compile pyproject.toml -o requirements.txt
```

## Releasing

### Create a Release

```bash
# Update version and run checks
just release 1.0.0

# Push to GitHub (triggers release workflow)
git push origin main v1.0.0
```

The `just release` command:
1. Updates version in `extension.toml` and `pyproject.toml`
2. Runs all checks and tests
3. Creates git commit and tag

### Automated Release Workflow

When you push a version tag, GitHub Actions:
1. Validates tag matches manifest version
2. Runs linting, type checking, and tests
3. Packages extension as tarball
4. Creates GitHub release with artifact

### Versioning (Semantic Versioning)

- **MAJOR** (1.0.0): Breaking changes
- **MINOR** (0.1.0): New features
- **PATCH** (0.0.1): Bug fixes

## Debugging

### Common Issues

**Extension won't start**
- Verify `config.json` exists and is valid
- Check required fields are present (`sensor_name`)
- Review logs for errors

**Actions not working**
- Ensure methods are decorated with `@action`
- Verify parameter types match decorators
- Check action is registered in `main.py`

**Tests failing**
- Run `just check` to catch linting/type errors
- Use `pytest -v` for verbose output
- Verify imports and fixtures

### Debug Logging

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Pull Requests

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make changes and commit: `git commit -m "feat: add feature"`
4. Push: `git push origin feature/my-feature`
5. Open a Pull Request

### PR Checklist

- [ ] Code follows style guidelines
- [ ] Tests pass (`just test`)
- [ ] No linting errors (`just check`)
- [ ] Documentation updated
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)

## Getting Help

- 📖 [Zelos Documentation](https://docs.zeloscloud.io/extensions)
- 🐛 [GitHub Issues](https://github.com/tkeairns/zelos-extension-can/issues)
- 📧 taylor@zeloscloud.io

## License

By contributing, you agree your contributions will be licensed under the MIT License.
