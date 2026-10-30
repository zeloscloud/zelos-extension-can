# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/getting-started/install-cli/) 0.1.12 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/), when the project has an agent runtime (`pyproject.toml`)
- Node.js 24 LTS (see `web/.nvmrc`), when the project has a web project (`web/package.json`)

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |
| Panels | `web/src/panels/<id>.html`, `web/src/panels/<id>/` | `[[app.panels]]` |

The web project builds every tab and panel document into `dist/` at the project root with
`just build` (`npm run build`). `.gitignore` keeps `dist/` out of the repository, so a fresh clone
has none until its first build.

The agent runtime is `zelos_extension_can/`. `zelos-can`, the CAN runtime package, owns the
buses, the codec, DBC and ARXML decode and the log converter; this package configures it:

| Module | Role |
| ------ | ---- |
| `cli/app.py` | App mode (no subcommand): reads the config, starts one codec per bus |
| `cli/trace.py`, `cli/convert.py`, `cli/export.py` | The `trace`, `convert` and `export` commands |
| `actions.py` | The actions the app and Zelos AI call, and the settings form's auto-configure |
| `gs_usb.py` | The optional gs_usb interface |
| `examples/` | Two scripts that trace a real bus to a `.trz` file |

The Bus Monitor panel is `web/src/panels/bus-monitor/`, a React app on
[`@zeloscloud/app-extension-sdk`](https://www.npmjs.com/package/@zeloscloud/app-extension-sdk):

| File | Role |
| ---- | ---- |
| `panel.tsx`, `columns.tsx`, `menu.ts` | The grid, its columns, its context menu |
| `use-bus-data.ts` | The two host subscriptions: the frame window and the latest decoded values |
| `data.ts` | Frames and decoded values to latched rows; the latch rules |
| `latch.ts` | The latch per panel in a workspace, kept in session storage across reloads |
| `options.ts` | The panel's options |
| `dev-feed.ts` | A synthetic bus for `just dev` |
| `grid/` | AG Grid chrome: search, cell rendering and status line, grid state |

## Commands

| Command                | Description                                                           |
| ---------------------- | --------------------------------------------------------------------- |
| `just install`         | Install dependencies and the pre-commit hooks                         |
| `just format`          | Format code with ruff and Prettier                                    |
| `just check`           | Check formatting and lint (ruff, Prettier, ESLint), type-check `web/` |
| `just test`            | Run tests (pytest, Vitest)                                            |
| `just build`           | Build the web project into `dist/`                                    |
| `just package`         | Build and package for the Zelos marketplace                           |
| `just release VERSION` | Bump version, check, test, build, commit, tag                         |
| `just clean`           | Remove build output, caches and packages                              |
| `just run`             | Run the agent runtime locally                                         |
| `just dev`             | Serve the panel against the SDK's mock host                           |
| `just watch`           | Rebuild `dist/` on every change                                       |

`just --list` also shows the recipes each part adds. `just install` installs the
pre-commit hooks in `.pre-commit-config.yaml`.

`just dev` serves `http://localhost:5173/panels/bus-monitor.html`. The SDK's mock host stands in
for Zelos, and `dev-feed.ts` feeds it a synthetic bus, so the grid fills without hardware or an
app.

## Run it locally

```bash
echo '{"buses": [{"interface": "Demo"}]}' > config.json
just run
```

`just run` reads `./config.json` and connects to the agent at `ZELOS_AGENT_URL`, else
`http://localhost:2300`. The `Demo` interface runs a built-in EV simulator and needs no hardware.

`zelos_extension_can/examples/` holds two scripts that trace a real bus to a `.trz` file and send
frame `0x100` every 100 ms. Run each with `uv run`.

- `socketcan.py`: Linux only. It uses `vcan0`, so create that interface first, or change the channel.
- `pcan.py`: needs the PCAN driver. Set the channel and bitrate for your adapter.

## Run it in Zelos

```bash
just build
zelos extensions install-local .
```

A local install runs from your project directory, so you do not install again after a code
change:

- Tab or panel: after a rebuild, reload the tab or the panel. Keep `just watch` running to rebuild
  on every save, and use `just dev` to work on it in the browser against the mock host of the SDK.
- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall local.can`.
- `extension.toml`: run `zelos extensions install-local .` again.

The local install's id is `local.can`:

```bash
zelos extensions start local.can --config '{"buses": [{"interface": "Demo"}]}'
zelos extensions logs local.can
```

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

```bash
just release 1.0.0
git push --follow-tags
```

The release workflow checks, tests, builds and packages the extension, then attaches the
archive to a GitHub release. CI runs the same check, test and package steps on every push and
pull request.

To list the extension in the Zelos marketplace, once: push the project to a GitHub repository,
submit it at [marketplace.zeloscloud.io](https://marketplace.zeloscloud.io/), and install the
Zelos GitHub App on the repository when asked. After approval, every release you publish reaches
the marketplace. See
[Publish to the Marketplace](https://docs.zeloscloud.io/sdk/how-to/package-extensions/#publish-to-the-marketplace).
