"""PyInstaller onedir build command for the optional desktop shell.

PyInstaller is deliberately not a runtime dependency.  The helper reports a
clear prerequisite message when a build environment does not have it, rather
than installing packages or silently producing an incomplete artifact.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# When invoked as ``python desktop/build.py`` Python puts only the desktop
# directory on sys.path.  Add the repository root before importing the package
# so the documented direct command works as well as ``python -m desktop.build``.
PACKAGING_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = Path(os.environ.get("AIDRAMA_SOURCE_ROOT", str(PACKAGING_ROOT))).resolve()
PROJECT_ROOT = PACKAGING_ROOT
if str(PACKAGING_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGING_ROOT))



# The executable is a shell around the local Streamlit service.  Freezing the
# Streamlit page itself would bypass the loopback/health/WebView lifecycle and
# produce a binary that cannot perform the documented desktop startup flow.
DESKTOP_ENTRYPOINT = Path(
    os.environ.get("AIDRAMA_DESKTOP_ENTRYPOINT", str(PACKAGING_ROOT / "desktop" / "launcher.py"))
).resolve()
PYWEBVIEW_VERSION = "6.2.1"


def _add_data(source: Path, destination: str) -> list[str]:
    """Return a platform-correct PyInstaller ``--add-data`` pair."""

    # PyInstaller uses ``;`` on Windows and ``:`` on POSIX, matching
    # ``os.pathsep``.  Keeping this in one helper makes the build definition
    # inspectable and avoids shell quoting differences.
    return ["--add-data", f"{source}{os.pathsep}{destination}"]


def runtime_data_args(source_root: Path | None = None) -> list[str]:
    """Data files needed by the Streamlit child at runtime.

    ``Main.py`` is intentionally shipped as data because the launcher invokes
    Streamlit with an explicit script path.  The package's Python modules are
    collected separately below; these entries cover the files loaded through
    ``Path(__file__)`` and the replaceable product mark.
    """

    source = Path(source_root or SOURCE_ROOT).resolve()
    package_root = source / "aidrama_studio"
    return [
        *_add_data(package_root / "Main.py", "aidrama_studio"),
        *_add_data(package_root / "styles.css", "aidrama_studio"),
        *_add_data(package_root / "assets", "aidrama_studio/assets"),
        # The frozen launcher copies this credential-free template into the
        # canonical AppData config directory on first start.  Keeping the
        # template in the bundle avoids writing user settings beside the EXE.
        *_add_data(source / "config.example.toml", "."),
        *_add_data(source / "LICENSE", "."),
        *_add_data(source / "NOTICE", "."),
        *_add_data(source / "THIRD_PARTY_NOTICES.md", "."),
        # Legacy MPT media services resolve bundled songs/public assets from
        # the bundle root. Proprietary system-font files are intentionally not
        # copied; the release audit rejects those names and Windows supplies a
        # user-installed fallback font.
        *_add_data(source / "resource" / "songs", "resource/songs"),
        *_add_data(source / "resource" / "public", "resource/public"),
    ]


def build_command(
    *,
    output_dir: Path | None = None,
    version: str | None = None,
    delivery_head: str | None = None,
    source_root: Path | None = None,
) -> list[str]:
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
    source = Path(source_root or SOURCE_ROOT).resolve()
    destination = output_dir or PROJECT_ROOT / "dist"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--name",
        "AIDramaStudio",
        "--distpath",
        str(destination),
        "--workpath",
        str(destination / "pyinstaller-build"),
        "--specpath",
        str(destination),
        "--paths",
        str(source),
        "--paths",
        str(DESKTOP_ENTRYPOINT.parent.parent),
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
        "--collect-all",
        "imageio_ffmpeg",
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
    # These values are passed as environment variables as well as recorded in
    # the post-build metadata.  Keeping them here makes the command itself
    # inspectable by CI and preserves compatibility with callers that only
    # need the legacy output_dir argument.
    if version:
        os.environ["AIDRAMA_VERSION"] = str(version)
    if delivery_head:
        os.environ["AIDRAMA_BUILD_SHA"] = str(delivery_head).lower()
    command.extend(runtime_data_args(source))
    command.append(str(DESKTOP_ENTRYPOINT))
    return command


def _parse_args(argv: list[str] | None = None):
    import argparse

    parser = argparse.ArgumentParser(description="Build the AIDrama Studio PyInstaller onedir package")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "dist")
    parser.add_argument("--version", required=False)
    parser.add_argument("--delivery-head", required=False)
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    return parser.parse_args(argv)


def _write_build_info(package_root: Path, *, version: str | None, delivery_head: str | None) -> Path:
    """Write a small, human-readable provenance file beside the frozen EXE."""

    package_root = Path(package_root).resolve()
    package_root.mkdir(parents=True, exist_ok=True)
    commit = (delivery_head or os.environ.get("AIDRAMA_BUILD_SHA") or "").strip().lower()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise RuntimeError("--delivery-head must be a full 40-character commit SHA")
    value = {
        "product_name": "AIDrama Studio",
        "product_version": (version or os.environ.get("AIDRAMA_VERSION") or "1.0.0").strip(),
        "delivery_head": commit,
        "build_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = package_root / "build-info.json"
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.version:
        os.environ["AIDRAMA_VERSION"] = str(args.version)
    if args.delivery_head:
        os.environ["AIDRAMA_BUILD_SHA"] = str(args.delivery_head).lower()
    try:
        command = build_command(
            output_dir=args.output_dir,
            version=args.version,
            delivery_head=args.delivery_head,
            source_root=args.source_root,
        )
    except RuntimeError as exc:
        print(f"AIDrama desktop build unavailable: {exc}", file=sys.stderr)
        return 2
    result = subprocess.call(command, cwd=str(PROJECT_ROOT))
    if result != 0:
        return result
    try:
        _write_build_info(
            args.output_dir / "AIDramaStudio",
            version=args.version,
            delivery_head=args.delivery_head,
        )
    except RuntimeError as exc:
        print(f"AIDrama build provenance unavailable: {exc}", file=sys.stderr)
        return 3
    try:
        from desktop.license_materials import collect_license_materials

        collect_license_materials(
            args.output_dir / "AIDramaStudio",
            lock_path=Path(args.source_root).resolve() / "uv.lock",
        )
    except Exception as exc:
        print(f"AIDrama license materials unavailable: {exc}", file=sys.stderr)
        return 3
    from desktop.release import ReleaseDefinitionError, write_package_release_metadata

    try:
        write_package_release_metadata(
            args.output_dir / "AIDramaStudio",
            git_commit=(args.delivery_head or os.environ.get("AIDRAMA_BUILD_SHA")),
            lock_path=Path(args.source_root).resolve() / "uv.lock",
        )
    except ReleaseDefinitionError as exc:
        print(f"AIDrama release metadata unavailable: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
