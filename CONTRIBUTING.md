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

## The Bus Monitor panel (`web/`)

The panel is a Vite + React project under `web/`. It runs inside the Zelos App's sandboxed panel frame and talks to the app only through [`@zeloscloud/app-extension-sdk`](https://www.npmjs.com/package/@zeloscloud/app-extension-sdk). `vite build` writes it to `dist/panels/bus-monitor.html`, the `entry` that `extension.toml` declares, and copies its options schema to `dist/panels/bus-monitor.options.json`.

| Path | What it holds |
|------|---------------|
| `web/src/panels/bus-monitor.html` | The panel's document |
| `web/src/panels/bus-monitor/data.ts` | Frames and decoded values to latched rows; the latch rules |
| `web/src/panels/bus-monitor/latch.ts` | The latch per panel instance, kept in sessionStorage across reloads |
| `web/src/panels/bus-monitor/use-bus-data.ts` | The two host subscriptions (frame window, latest values) |
| `web/src/panels/bus-monitor/panel.tsx`, `columns.tsx`, `menu.ts` | The grid, its columns, its context menu |
| `web/src/panels/bus-monitor/grid/` | Search, cell chrome and status line, grid state |
| `web/public/panels/bus-monitor.options.json` | The options the panel's Edit sheet shows |

### Setup

Node 22 or later.

```bash
just web-install                       # npm install in web/
just web-install-local path/to/sdk.tgz # or: against a local build of the SDK
```

`web/package.json` pins `@zeloscloud/app-extension-sdk` to `^0.4.0`. Until that version is on npm, install a local tarball with `just web-install-local`; it leaves `package.json` unchanged. The repository holds no `web/package-lock.json` yet: once 0.4.0 is published, drop `--no-package-lock` from the `web-install` recipe, remove the lockfile's line from `.gitignore`, and commit the lockfile `just web-install` writes.

### Dev loop

```bash
just web-dev    # http://localhost:5173/panels/bus-monitor.html
```

Outside Zelos the SDK runs against its mock host, and the panel feeds it a synthetic bus (`dev-feed.ts`), so the grid fills without hardware or an app.

To see the panel in the app:

```bash
just web-watch                         # rebuild dist/ on every change
zelos extensions install-local .       # once, in another terminal
```

Then, after a change, pick **Reload** in the panel's ⋮ menu in the app.

### Checks and tests

```bash
just web-check   # tsc
just web-test    # vitest: the data rules, the latch, and the panel against the SDK's mock host
```

`just check` and `just test` run these after the Python checks, and `just package` builds the panel first.

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

This builds the panel, then creates a `.tar.gz` file ready to upload to the Zelos Marketplace (automatically happens in CI!). The archive holds every path in `[package].paths` of `extension.toml`, `dist/` included, plus `actions.json`.

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
