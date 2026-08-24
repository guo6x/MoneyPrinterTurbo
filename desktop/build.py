"""PyInstaller onedir build command for the optional desktop shell.

PyInstaller is deliberately not a runtime dependency.  The helper reports a
clear prerequisite message when a build environment does not have it, rather
than installing packages or silently producing an incomplete artifact.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

# When invoked as ``python desktop/build.py`` Python puts only the desktop
# directory on sys.path.  Add the repository root before importing the package
# so the documented direct command works as well as ``python -m desktop.build``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from desktop.launcher import DEFAULT_MAIN


def build_command(*, output_dir: Path | None = None) -> list[str]:
    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError("PyInstaller is not installed; install only the optional desktop build tool to package AIDrama")
    destination = output_dir or PROJECT_ROOT / "dist"
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onedir",
        "--name",
        "AIDramaStudio",
        "--distpath",
        str(destination),
        str(DEFAULT_MAIN),
    ]


def main() -> int:
    try:
        command = build_command()
    except RuntimeError as exc:
        print(f"AIDrama desktop build unavailable: {exc}", file=sys.stderr)
        return 2
    return subprocess.call(command, cwd=str(PROJECT_ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
