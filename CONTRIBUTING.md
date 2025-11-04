# Contributing

## First Commit

All files are staged and ready. Create your first commit:

```bash
git commit -m "Initial commit"
```

## Push to GitHub

Create a repository on GitHub, then:

```bash
git remote add origin git@github.com:zeloscloud/zelos-extension-can.git
git push -u origin main
```

## Development Workflow

```bash
just install  # Install dependencies and setup
just dev      # Run locally
just test     # Run tests
just check    # Lint code
```

1. Make your changes
2. Run `just format` to auto-format
3. Run `just test` to verify tests pass
4. Commit your changes (pre-commit hooks run automatically)

## Common Tasks

### Run Locally

```bash
just dev
```

Press Ctrl+C to stop.

### Add a Dependency

```bash
uv add package-name        # Runtime dependency
uv add --dev package-name  # Dev dependency
```

### Package for Marketplace

```bash
just package
```

This creates a `.tar.gz` file ready to upload to the Zelos Marketplace (automatically happens in CI!)

### Create a Release

```bash
just release 1.0.0
git push --follow-tags
```

This updates version numbers, runs tests, and creates a git tag.

## Testing

### Write Tests

```python
# tests/test_feature.py
from delete_this_later.extension import MyExtension

def test_something():
    extension = MyExtension({"setting": "value"})
    assert extension.do_something() == expected_result
```

### Run Tests

```bash
just test           # Run all tests
uv run pytest -v    # Verbose output
uv run pytest -k test_name  # Run specific test
```

## Code Quality

### Formatting & Linting

```bash
just format  # Auto-fix formatting
just check   # Check for issues
```

Pre-commit hooks run automatically on `git commit` and will:
- Format code with ruff
- Check for common issues
- Validate YAML/TOML/JSON files

### Type Hints

Use type hints on all function signatures:

```python
def my_function(name: str, count: int) -> list[str]:
    return [name] * count
```

## Getting Help

- [Zelos Docs](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-can/issues)

## License

MIT - see [LICENSE](LICENSE)
