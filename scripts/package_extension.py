#!/usr/bin/env python3
"""Package the Zelos extension into a tar.gz archive."""

import subprocess
import sys
import tarfile
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore


def filter_pycache(tarinfo):
    """Filter out Python cache files.

    :param tarinfo: Tar member info
    :return: None if should be excluded, tarinfo otherwise
    """
    if "__pycache__" in tarinfo.name or tarinfo.name.endswith((".pyc", ".pyo")):
        return None
    return tarinfo


def main():
    """Package the extension."""
    # Load manifest
    try:
        with open("extension.toml", "rb") as f:
            manifest = tomllib.load(f)
    except FileNotFoundError:
        print("ERROR: extension.toml not found")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to parse extension.toml: {e}")
        sys.exit(1)

    version = manifest.get("version")
    if not version:
        print("ERROR: No version in extension.toml")
        sys.exit(1)

    # Ensure requirements.txt exists
    if not Path("requirements.txt").exists():
        print("Compiling requirements.txt...")
        result = subprocess.run(
            ["uv", "pip", "compile", "pyproject.toml", "-o", "requirements.txt"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("ERROR: Failed to compile requirements.txt")
            print(result.stderr)
            sys.exit(1)

    # Collect files to package
    files = ["extension.toml"]  # Always required

    # Add runtime files from manifest
    runtime = manifest.get("runtime", {})
    if "entry" in runtime:
        files.append(runtime["entry"])
    if "requirements" in runtime:
        files.append(runtime["requirements"])

    # Add optional files referenced in manifest
    for key in ["icon", "readme", "changelog"]:
        if key in manifest:
            files.append(manifest[key])

    # Add config schema if present
    config = manifest.get("config", {})
    if "schema" in config:
        files.append(config["schema"])

    # Add Python packages (directories with __init__.py)
    for path in Path(".").iterdir():
        if (
            path.is_dir()
            and (path / "__init__.py").exists()
            and path.name not in ["tests", "test", "__pycache__"]
        ):
            files.append(path.name)

    # Create archive
    project_name = Path.cwd().name
    archive_name = f"{project_name}-v{version}.tar.gz"

    print(f"Creating {archive_name}...")
    with tarfile.open(archive_name, "w:gz") as tar:
        for file_path in sorted(set(files)):
            path = Path(file_path)
            if not path.exists():
                print(f"ERROR: Required file missing: {file_path}")
                sys.exit(1)

            tar.add(file_path, arcname=file_path, filter=filter_pycache)
            print(f"  + {file_path}")

    size_kb = Path(archive_name).stat().st_size / 1024
    print(f"\n✓ Package created: {archive_name} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
