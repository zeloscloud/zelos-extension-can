set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

default:
    @just --list

# Install dependencies and set up development environment
install:
    uv sync --extra dev
    uv run pre-commit install

# Format and fix code
format:
    uv run ruff format .
    uv run ruff check --fix .

# Run linting
check:
    uv run ruff check .

# Run tests
test:
    uv run pytest

# Run the extension locally
dev:
    uv run python main.py

# Package the extension for distribution
package:
    uv run python scripts/package_extension.py

# Create a new release
release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail

    # Bump version using Python script
    uv run python scripts/bump_version.py {{VERSION}}

    # Run checks
    just check
    just test

    # Commit and tag (only if there are changes)
    git add extension.toml pyproject.toml
    if git diff --staged --quiet; then
        echo "Version already set to {{VERSION}}, creating tag only"
    else
        git commit -m "Release v{{VERSION}}"
    fi
    git tag "v{{VERSION}}"

    echo ""
    echo "✓ Release v{{VERSION}} ready!"
    echo ""
    echo "Push with:"
    echo "  git push origin main v{{VERSION}}"

# Clean build artifacts
clean:
    rm -rf dist build .pytest_cache .ruff_cache
    rm -rf zelos-extension-can-v*.tar.gz
    rm -rf .artifacts
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
