"""PyInstaller onedir build command for the optional desktop shell.

PyInstaller is deliberately not a runtime dependency.  The helper reports a
clear prerequisite message when a build environment does not have it, rather
than installing packages or silently producing an incomplete artifact.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

# When invoked as ``python desktop/build.py`` Python puts only the desktop
# directory on sys.path.  Add the repository root before importing the package
# so the documented direct command works as well as ``python -m desktop.build``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))



# The executable is a shell around the local Streamlit service.  Freezing the
# Streamlit page itself would bypass the loopback/health/WebView lifecycle and
# produce a binary that cannot perform the documented desktop startup flow.
DESKTOP_ENTRYPOINT = PROJECT_ROOT / "desktop" / "launcher.py"
PYWEBVIEW_VERSION = "6.2.1"


def _add_data(source: Path, destination: str) -> list[str]:
    """Return a platform-correct PyInstaller ``--add-data`` pair."""

    # PyInstaller uses ``;`` on Windows and ``:`` on POSIX, matching
    # ``os.pathsep``.  Keeping this in one helper makes the build definition
    # inspectable and avoids shell quoting differences.
    return ["--add-data", f"{source}{os.pathsep}{destination}"]


def runtime_data_args() -> list[str]:
    """Data files needed by the Streamlit child at runtime.

    ``Main.py`` is intentionally shipped as data because the launcher invokes
    Streamlit with an explicit script path.  The package's Python modules are
    collected separately below; these entries cover the files loaded through
    ``Path(__file__)`` and the replaceable product mark.
    """

    package_root = PROJECT_ROOT / "aidrama_studio"
    return [
        *_add_data(package_root / "Main.py", "aidrama_studio"),
        *_add_data(package_root / "styles.css", "aidrama_studio"),
        *_add_data(package_root / "assets", "aidrama_studio/assets"),
        # The frozen launcher copies this credential-free template into the
        # canonical AppData config directory on first start.  Keeping the
        # template in the bundle avoids writing user settings beside the EXE.
        *_add_data(PROJECT_ROOT / "config.example.toml", "."),
        *_add_data(PROJECT_ROOT / "LICENSE", "."),
        *_add_data(PROJECT_ROOT / "NOTICE", "."),
        *_add_data(PROJECT_ROOT / "THIRD_PARTY_NOTICES.md", "."),
    ]


def build_command(*, output_dir: Path | None = None) -> list[str]:
    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError("PyInstaller is not installed; install only the optional desktop build tool to package AIDrama")
    try:
        installed_webview = importlib.metadata.version("pywebview")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"PyWebView {PYWEBVIEW_VERSION} is required for the native desktop build"
        ) from exc
    if installed_webview != PYWEBVIEW_VERSION:
        raise RuntimeError(
            f"PyWebView {PYWEBVIEW_VERSION} is required for the native desktop build; "
            f"found {installed_webview}"
        )
    destination = output_dir or PROJECT_ROOT / "dist"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onedir",
        "--name",
        "AIDramaStudio",
        "--distpath",
        str(destination),
        "--paths",
        str(PROJECT_ROOT),
        # Streamlit and the product services load package resources and a few
        # modules dynamically.  Collecting these namespaces keeps the frozen
        # shell faithful to the source launcher without changing application
        # architecture or adding runtime dependencies.
        "--collect-all",
        "streamlit",
        # PyWebView is imported lazily by the launcher. Collect its WinForms
        # backend and bundled WebView2 interop DLLs explicitly so a frozen
        # executable opens a native window instead of silently falling back
        # to the system browser.
        "--collect-all",
        "webview",
        "--collect-all",
        "pythonnet",
        "--collect-all",
        "clr_loader",
        "--collect-all",
        "proxy_tools",
        "--collect-submodules",
        "aidrama_studio",
        "--collect-submodules",
        "app",
        "--collect-submodules",
        "moviepy",
        # imageio asks importlib.metadata for its distribution version during
        # media-engine checks.  PyInstaller bundles code but not dist-info by
        # default, so preserve this tiny metadata record in the physical tree.
        "--copy-metadata",
        "imageio",
        "--hidden-import",
        "app.services.video",
        "--hidden-import",
        "app.utils.utils",
        "--hidden-import",
        "webview.platforms.winforms",
        "--hidden-import",
        "webview.platforms.edgechromium",
        "--hidden-import",
        "webview.platforms.mshtml",
        "--hidden-import",
        "clr",
    ]
    command.extend(runtime_data_args())
    command.append(str(DESKTOP_ENTRYPOINT))
    return command


def main() -> int:
    try:
        command = build_command()
    except RuntimeError as exc:
        print(f"AIDrama desktop build unavailable: {exc}", file=sys.stderr)
        return 2
    result = subprocess.call(command, cwd=str(PROJECT_ROOT))
    if result != 0:
        return result
    from desktop.release import ReleaseDefinitionError, write_package_release_metadata

    try:
        write_package_release_metadata(PROJECT_ROOT / "dist" / "AIDramaStudio")
    except ReleaseDefinitionError as exc:
        print(f"AIDrama release metadata unavailable: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
