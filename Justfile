set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

default:
    @just --list

# Install dependencies
install:
    uv sync --extra dev
    uv run pre-commit install

# Install dependencies for CI (no pre-commit hooks)
ci-install:
    uv sync --extra dev

# Format code
format:
    uv run ruff format .
    uv run ruff check --fix .

# Run checks: ruff on the agent, tsc on the panels
check: web-check
    uv run ruff check .

# Run tests: the agent's, then the panels'
test: web-test
    uv run pytest

# Run extension locally
dev:
    uv run python main.py

# Package extension (builds the panels first)
package: web-build
    uv run python scripts/package_extension.py

# Install the panels' npm dependencies
web-install:
    (cd web && npm install --no-package-lock)

# Install the panels' dependencies against a local SDK tarball (package.json keeps its ^ range)
web-install-local SDK_TGZ:
    cd web && npm install --no-save --no-package-lock "$(realpath "{{SDK_TGZ}}")"

# Build the panels into dist/panels/
web-build:
    (cd web && npm run build)

# Type-check the panels
web-check:
    (cd web && npm run check)

# Run the panels' unit tests
web-test:
    (cd web && npm test)

# Serve the panels against the SDK's mock host: http://localhost:5173/panels/bus-monitor.html
web-dev:
    (cd web && npm run dev)

# Rebuild dist/ on every change, for a local install in Zelos
web-watch:
    (cd web && npm run watch)

# Release new version
release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail

    VERSION="{{VERSION}}"

    # Ensure clean working directory
    git diff --quiet && git diff --staged --quiet || (echo "Error: Uncommitted changes" && exit 1)

    # Update versions
    uv run python scripts/bump_version.py "$VERSION"

    # Format and update dependencies
    just format
    uv lock

    # Run tests
    just test

    # Commit everything
    git add -A
    git commit -m "Release v$VERSION"
    git tag -a "v$VERSION" -m "Release v$VERSION"

    echo ""
    echo "✓ Release v$VERSION ready!"
    echo ""
    echo "Push with: git push --follow-tags"

# Clean build artifacts
clean:
    rm -rf dist build .pytest_cache .ruff_cache *.tar.gz .artifacts actions.json
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
