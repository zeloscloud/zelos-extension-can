set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

default:
    @just --list

# Install dependencies and set up development environment
install:
    uv sync --extra dev
    uv run pre-commit install
    uv pip compile pyproject.toml -o requirements.txt

# Format and fix code
format:
    uv run ruff format .
    uv run ruff check --fix .

# Run linting and type checking
check:
    uv run ruff check .
    uv run ty check

# Run tests
test:
    uv run pytest

# Run the extension locally
dev:
    uv run python main.py

# Run the extension in demo mode with built-in EV simulator
demo:
    @echo "Starting CAN extension in demo mode..."
    @echo "Demo features:"
    @echo "  - Physics-based EV simulator"
    @echo "  - Realistic CAN message traffic"
    @echo "  - Uses bundled demo.dbc"
    @echo ""
    @echo "Press Ctrl+C to stop"
    @echo ""
    uv run python main.py --demo

# Package the extension for distribution
package:
    uv pip compile pyproject.toml -o requirements.txt
    uv run python scripts/package_extension.py

# Create a new release
release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate version format
    if ! [[ "{{VERSION}}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "Error: Version must be in format X.Y.Z"
        exit 1
    fi

    # Update version in files (only top-level version, not [zelos].version)
    sed -i.bak '/^\[zelos\]/,/^\[/!s/^version = ".*"/version = "{{VERSION}}"/' extension.toml && rm extension.toml.bak
    sed -i.bak 's/^version = ".*"/version = "{{VERSION}}"/' pyproject.toml && rm pyproject.toml.bak

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

    echo "Release v{{VERSION}} ready. Push with:"
    echo "  git push origin main v{{VERSION}}"

# Clean build artifacts
clean:
    rm -rf dist build .pytest_cache .ty_cache .ruff_cache
    rm -rf zelos-extension-can-v*.tar.gz requirements.txt
    rm -rf .artifacts
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
