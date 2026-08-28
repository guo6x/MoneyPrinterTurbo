"""Hash-pinned Windows FFmpeg materialization for the desktop package.

This module owns the *packaging* FFmpeg boundary.  It intentionally does not
consult ``imageio_ffmpeg`` or PATH: the installer receives only the exact
Windows x64 shared build below, after its archive hash and encoder inventory
have been checked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


FFMPEG_DISTRIBUTION = {
    "provider": "BtbN/FFmpeg-Builds",
    "release": "latest (asset content is pinned by SHA-256)",
    "asset_id": 532622813,
    "asset_name": "ffmpeg-n8.1-latest-win64-lgpl-shared-8.1.zip",
    "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-lgpl-shared-8.1.zip",
    "sha256": "54b56d8f7e3fdeb3a987650a93cf4d4ed2f446f893f109dce191deec2007d155",
    "source_repository": "https://github.com/BtbN/FFmpeg-Builds",
    "ffmpeg_source": "https://ffmpeg.org/download.html",
    "configured_h264_encoder": "h264_mf",
}

_ENCODER_LINE = re.compile(r"(?m)^\s*[A-Z.]+\s+{encoder}(?:\s|$)")


class FFmpegDistributionError(RuntimeError):
    """The selected binary cannot meet the package's fixed media contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_expected_archive(path: Path) -> Path:
    archive = Path(path).resolve()
    if not archive.is_file():
        raise FFmpegDistributionError(f"FFmpeg archive is missing: {archive}")
    actual_hash = sha256_file(archive)
    if actual_hash != FFMPEG_DISTRIBUTION["sha256"]:
        raise FFmpegDistributionError(
            "FFmpeg archive SHA-256 mismatch; refusing an unpinned binary: "
            f"expected {FFMPEG_DISTRIBUTION['sha256']}, got {actual_hash}"
        )
    return archive


def fetch_pinned_archive(destination: Path) -> Path:
    """Download the reviewed asset and accept it only when its hash matches."""

    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.name}.", suffix=".download", dir=target.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        request = urllib.request.Request(
            str(FFMPEG_DISTRIBUTION["url"]),
            headers={"User-Agent": "AIDrama-Windows-Packaging/1.0"},
        )
        with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        _require_expected_archive(temporary)
        temporary.replace(target)
        return target
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _safe_extract(archive: Path, destination: Path) -> None:
    """Extract a verified zip without allowing archive paths to escape staging."""

    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            member = PurePosixPath(entry.filename)
            if member.is_absolute() or ".." in member.parts:
                raise FFmpegDistributionError("FFmpeg archive contains an unsafe member path")
        source.extractall(destination)


def _run(binary: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            [str(binary), *arguments],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FFmpegDistributionError(f"cannot inspect bundled FFmpeg: {exc}") from exc
    output = f"{completed.stdout}\n{completed.stderr}".strip() + "\n"
    if completed.returncode != 0:
        raise FFmpegDistributionError(
            f"bundled FFmpeg {' '.join(arguments)} failed with exit {completed.returncode}"
        )
    return output


def _has_encoder(inventory: str, encoder: str) -> bool:
    return re.search(
        _ENCODER_LINE.pattern.format(encoder=re.escape(encoder)), inventory
    ) is not None


def _find_binary_root(extracted: Path) -> tuple[Path, Path, Path]:
    ffmpegs = sorted(extracted.rglob("ffmpeg.exe"), key=lambda path: path.as_posix())
    ffprobes = sorted(extracted.rglob("ffprobe.exe"), key=lambda path: path.as_posix())
    if len(ffmpegs) != 1 or len(ffprobes) != 1:
        raise FFmpegDistributionError("reviewed archive must contain exactly one ffmpeg.exe and ffprobe.exe")
    ffmpeg, ffprobe = ffmpegs[0], ffprobes[0]
    if ffmpeg.parent != ffprobe.parent:
        raise FFmpegDistributionError("FFmpeg and FFprobe must share one binary directory")
    return ffmpeg, ffprobe, ffmpeg.parent.parent


def stage_distribution(archive_path: Path, destination: Path) -> Path:
    """Validate and stage only the runtime files for the selected FFmpeg build.

    The returned directory is suitable for a single PyInstaller ``--add-data``
    entry.  It contains FFmpeg, FFprobe, every shared dependency DLL, the
    upstream LGPL text, and immutable technical evidence from the same binary.
    """

    archive = _require_expected_archive(Path(archive_path))
    target = Path(destination).resolve()
    if target.exists():
        raise FFmpegDistributionError(f"FFmpeg staging destination already exists: {target}")
    target.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="aidrama-ffmpeg-extract-") as temporary:
        extracted = Path(temporary)
        _safe_extract(archive, extracted)
        ffmpeg, ffprobe, archive_root = _find_binary_root(extracted)
        version = _run(ffmpeg, "-version")
        buildconf = _run(ffmpeg, "-buildconf")
        license_output = _run(ffmpeg, "-L")
        encoders = _run(ffmpeg, "-hide_banner", "-encoders")
        configured_encoder = str(FFMPEG_DISTRIBUTION["configured_h264_encoder"])
        h264_mf_present = _has_encoder(encoders, configured_encoder)
        libx264_present = _has_encoder(encoders, "libx264")
        gpl_enabled = "--enable-gpl" in buildconf
        if not h264_mf_present:
            raise FFmpegDistributionError("reviewed FFmpeg does not expose h264_mf")
        if libx264_present or "--enable-libx264" in buildconf:
            raise FFmpegDistributionError("reviewed FFmpeg exposes libx264; refusing GPL/x264 payload")
        if gpl_enabled or "GNU General Public License" in license_output:
            raise FFmpegDistributionError("reviewed FFmpeg reports GPL; refusing this package payload")
        if "GNU Lesser General Public License" not in license_output:
            raise FFmpegDistributionError("reviewed FFmpeg does not self-report LGPL licensing")

        for source in sorted(ffmpeg.parent.iterdir(), key=lambda path: path.name.casefold()):
            if source.is_file() and (source.suffix.casefold() == ".dll" or source.name.casefold() in {"ffmpeg.exe", "ffprobe.exe"}):
                shutil.copy2(source, target / source.name)
        license_source = archive_root / "LICENSE.txt"
        if not license_source.is_file():
            raise FFmpegDistributionError("reviewed FFmpeg archive lacks LICENSE.txt")
        shutil.copy2(license_source, target / "LICENSE.txt")
        payload = [
            {"path": path.name, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
            for path in sorted(target.iterdir(), key=lambda path: path.name.casefold())
            if path.is_file()
        ]
        evidence = {
            "schema_version": 1,
            "distribution": FFMPEG_DISTRIBUTION,
            "archive_sha256": sha256_file(archive),
            "ffmpeg_version": version,
            "ffmpeg_buildconf": buildconf,
            "ffmpeg_license_output": license_output,
            "ffmpeg_encoders": encoders,
            "technical_license_assessment": {
                "license_class": "LGPLv3 as reported by ffmpeg -L",
                "gpl_components_present": False,
                "gpl_evidence": "ffmpeg -L is LGPL and -buildconf contains no --enable-gpl",
                "libx264_present": False,
                "libx264_evidence": "libx264 absent from -encoders and --enable-libx264 absent from -buildconf",
                "h264_mf_present": True,
                "h264_mf_evidence": "h264_mf present in ffmpeg -encoders",
                "legal_approval": "NOT_A_LEGAL_APPROVAL",
            },
            "payload": payload,
        }
        (target / "distribution-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return target


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch or validate AIDrama's pinned Windows FFmpeg payload")
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch", help="download the pinned archive and verify SHA-256")
    fetch.add_argument("--output", required=True, type=Path)
    stage = subparsers.add_parser("stage", help="validate an archive and stage its package payload")
    stage.add_argument("--archive", required=True, type=Path)
    stage.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "fetch":
            print(fetch_pinned_archive(args.output))
        else:
            print(stage_distribution(args.archive, args.output))
    except FFmpegDistributionError as exc:
        print(f"AIDrama FFmpeg distribution unavailable: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FFMPEG_DISTRIBUTION",
    "FFmpegDistributionError",
    "fetch_pinned_archive",
    "sha256_file",
    "stage_distribution",
]
