"""Export the optional service as an independent Python project without host code."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def export(destination: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "agent_harness"
    if destination.exists():
        raise ValueError("Choose a new output directory; existing files are never replaced")
    destination.mkdir(parents=True)
    shutil.copytree(
        source,
        destination / "agent_harness",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    shutil.copyfile(source / "standalone.toml", destination / "pyproject.toml")
    shutil.copyfile(source / "README.md", destination / "README.md")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    export(args.destination.resolve())
    print(f"Harness exported to {args.destination.resolve()}")


if __name__ == "__main__":
    main()
