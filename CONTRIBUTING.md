# Contributing to Zelos Extension Can

Thank you for contributing! This guide covers the development workflow and project structure.

## Quick Reference

**Common tasks:**
```bash
just install        # First-time setup
just dev            # Test locally
just format         # Fix formatting
just check          # Lint code
just test           # Run tests
just release 1.0.0  # Create release
```

**Need help?** See [Table of Contents](#table-of-contents) below.

## Table of Contents

- [Development Setup](#development-setup)
- [Development Workflow](#development-workflow)
- [Project Structure](#project-structure)
- [Testing](#testing)
- [Code Quality](#code-quality)
- [Dependencies](#dependencies)
- [Packaging](#packaging)
- [Releasing](#releasing)
- [Debugging](#debugging)
- [Pull Requests](#pull-requests)
- [Getting Help](#getting-help)

## Development Setup

### Prerequisites

- Python 3.11+
- [UV](https://github.com/astral-sh/uv) - Fast Python package manager
- [just](https://github.com/casey/just) - Command runner (`brew install just` on macOS)
- Git

### Getting Started

```bash
# Install dependencies and verify setup
just install
just check
just test
```

This installs all dependencies, sets up pre-commit hooks, and verifies everything works.

### GitHub Repository Setup

To push your new project to GitHub, first create a repository named `zelos-extension-can` on GitHub, then:

```bash
git remote add origin git@github.com:tkeairns/zelos-extension-can.git
git branch -M main
git push -u origin main
```

**Note:** The repository is already initialized with Git and has an initial commit from the template.

## Development Workflow

### Available Commands

| Command | Purpose |
|---------|---------|
| `just install` | Install dependencies and pre-commit hooks |
| `just dev` | Run extension locally |
| `just format` | Auto-format code with ruff |
| `just check` | Run linting with ruff |
| `just test` | Run test suite with pytest |
| `just package` | Create distribution tarball |
| `just release VERSION` | Create a new release (e.g., `just release 1.0.0`) |
| `just clean` | Clean build artifacts |

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
├── uv.lock                     # Locked dependencies (auto-generated)
├── Justfile                    # Development commands
├── zelos_extension_can/
│   ├── __init__.py
│   ├── extension.py            # Core: SensorMonitor class with actions
│   └── utils/
│       └── __init__.py         # Utility modules (add as needed)
├── tests/
│   └── test_extension.py       # Unit tests
├── assets/
│   └── icon.svg                # Marketplace icon
├── scripts/
│   └── package_extension.py    # Packaging for marketplace
├── .vscode/
│   ├── settings.json           # VSCode settings
│   └── extensions.json         # Recommended extensions
└── .github/
    ├── workflows/
    │   ├── CI.yml              # CI on pushes/PRs
    │   └── release.yml         # Release automation on tags
    └── dependabot.yml          # Automated dependency updates
```

### Key Files

- **`extension.toml`**: Extension metadata, version, and runtime config
- **`main.py`**: Entry point with signal handlers and SDK initialization
- **`zelos_extension_can/extension.py`**: Core monitor class with lifecycle, actions, and data streaming
- **`config.schema.json`**: JSON Schema defining configuration UI in Zelos App

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


def test_feature():
    config = {"sensor_name": "test", "interval": 0.1}
    monitor = SensorMonitor(config)

    monitor.start()
    assert monitor.running is True
    monitor.stop()
    assert monitor.running is False
```

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
- YAML/TOML validation
- Trailing whitespace removal

Run manually:
```bash
pre-commit run --all-files
```

## Dependencies

Dependencies are managed via `pyproject.toml` and locked in `uv.lock`.

```bash
# Add runtime dependency
uv add package-name

# Add dev dependency
uv add --dev package-name

# Dependencies are automatically locked in uv.lock
# The Zelos runtime will install from pyproject.toml + uv.lock
```

## Packaging

The `scripts/package_extension.py` script creates marketplace-ready tar.gz archives. It can be customized for your extension's specific needs.

### Basic Usage

```bash
# Package extension for marketplace
just package

# This creates: zelos-extension-can-v0.1.0.tar.gz
```

### Customizing Package Contents

#### Include Additional Files

Add custom files after the automatic collection logic:

```python
# In scripts/package_extension.py, in main() after collecting Python packages
files.append("custom_config.yaml")
files.append("assets/custom_icon.svg")
files.append("binaries/")  # Include entire directory
```

#### Exclude Specific Files from Packages

Add filtering logic to skip certain paths:

```python
# In filter_archive_files() function
def filter_archive_files(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Filter out unwanted files from archive per Zelos security requirements."""

    # Existing filters...

    # Add custom exclusions
    if tarinfo.name.startswith("dev_tools/"):
        return None
    if tarinfo.name.endswith(".test.py"):
        return None

    return tarinfo
```

#### Platform-Specific Packaging

Include different files based on the target platform:

```python
import platform

# After collecting base files
if platform.system() == "Windows":
    files.append("drivers/windows_driver.dll")
elif platform.system() == "Linux":
    files.append("drivers/linux_driver.so")
elif platform.system() == "Darwin":
    files.append("drivers/macos_driver.dylib")
```

#### Increase Compression

For larger extensions, use maximum compression:

```python
# In scripts/package_extension.py, in main() when opening the tarfile
# Change from:
with tarfile.open(archive_name, "w:gz") as tar:

# To:
with tarfile.open(archive_name, "w:gz", compresslevel=9) as tar:
```

#### Pre-Package Build Steps

Add build steps before packaging:

```python
def main() -> None:
    """Package the extension."""

    # Add custom build steps
    print("Compiling Python to bytecode...")
    subprocess.run(["python", "-m", "compileall", "zelos_extension_can"], check=True)

    print("Minifying assets...")
    subprocess.run(["uglifyjs", "assets/script.js", "-o", "assets/script.min.js"], check=True)

    # Continue with normal packaging...
    manifest = tomllib.load(open("extension.toml", "rb"))
    ...
```

### Security Requirements

The packaging script automatically filters:
- Python cache files (`__pycache__`, `.pyc`, `.pyo`)
- Hidden files and directories (starting with `.`)
- Symlinks and special files (security requirement)

These filters ensure marketplace compliance. Do not remove them.

### Size Limits

- **Maximum archive size**: 500 MB
- The script validates size before completion
- If you exceed the limit:
  - Remove unnecessary files
  - Compress assets (images, videos)
  - Consider splitting into multiple extensions
  - Use external downloads for large assets

### Troubleshooting Packaging

**"ERROR: Required file missing"**
- Ensure all files referenced in `extension.toml` exist
- Verify `pyproject.toml` and `uv.lock` are present

**"Dependencies not installing"**
- Ensure `pyproject.toml` has valid dependencies
- Check that `uv.lock` exists (run `uv sync` to generate)
- The Zelos runtime will install from `pyproject.toml` automatically

**Archive too large**
- Check what's included: `tar -tzf zelos-extension-can-v*.tar.gz`
- Look for accidentally included files (`.venv`, `node_modules`, etc.)
- Verify `.gitignore` patterns are working

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
