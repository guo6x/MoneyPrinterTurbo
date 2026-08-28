"""Collect release license materials from the build environment.

This helper runs as part of the packaging build, using the same dedicated
Python interpreter that PyInstaller uses.  It copies license/notice files from
the installed runtime wheels into the package and records the exact FFmpeg
binary's self-reported license/build information.  It never invents a license
grant: an absent license file is recorded as a release-review item.
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

from desktop.release import locked_runtime_components


BUILD_TOOLS = (
    "imageio-ffmpeg",
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "pywebview",
    "streamlit",
)
LICENSE_MARKERS = ("license", "copying", "notice", "copyright", "authors")


def _safe_name(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value).strip("_")


def _license_files(distribution: metadata.Distribution) -> list[Path]:
    result: list[Path] = []
    for item in distribution.files or ():
        relative = Path(str(item))
        lowered = "/".join(relative.parts).casefold()
        basename = relative.name.casefold()
        if any(marker in basename or f"/{marker}" in lowered for marker in LICENSE_MARKERS):
            candidate = Path(distribution.locate_file(relative))
            if candidate.is_file():
                result.append(candidate)
    return sorted(set(result), key=lambda path: path.as_posix().casefold())


def _copy_distribution_materials(root: Path, package_name: str, destination: Path) -> dict[str, object]:
    try:
        distribution = metadata.distribution(package_name)
    except metadata.PackageNotFoundError:
        return {"name": package_name, "status": "MISSING_FROM_BUILD_ENVIRONMENT", "files": []}
    target = destination / _safe_name(package_name)
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for source in _license_files(distribution):
        # Keep files flat and deterministic while preserving the source name.
        filename = source.name
        candidate = target / filename
        if candidate.exists():
            filename = f"{source.parent.name}-{filename}"
            candidate = target / filename
        shutil.copyfile(source, candidate)
        copied.append(candidate.relative_to(root).as_posix())
    meta = {
        "name": distribution.metadata.get("Name") or package_name,
        "version": distribution.version,
        "license": distribution.metadata.get("License"),
        "home_page": distribution.metadata.get("Home-page"),
        "status": "LICENSE_FILES_COPIED" if copied else "NO_LICENSE_FILE_IN_DISTRIBUTION",
        "files": copied,
    }
    (target / "distribution-metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return meta


def _collect_ffmpeg(root: Path, destination: Path) -> dict[str, object]:
    """Record the FFmpeg binary that is physically inside this package."""

    target = destination / "ffmpeg"
    target.mkdir(parents=True, exist_ok=True)
    bundled = root / "_internal" / "ffmpeg"
    binary = bundled / "ffmpeg.exe"
    ffprobe = bundled / "ffprobe.exe"
    evidence_path = bundled / "distribution-evidence.json"
    if not binary.is_file() or not ffprobe.is_file() or not evidence_path.is_file():
        raise RuntimeError("reviewed FFmpeg payload is missing from the physical package tree")
    license_files: list[str] = []
    for filename in ("LICENSE.txt", "distribution-evidence.json"):
        source = bundled / filename
        if not source.is_file():
            raise RuntimeError(f"reviewed FFmpeg payload lacks {filename}")
        copied = target / filename
        shutil.copyfile(source, copied)
        license_files.append(copied.relative_to(root).as_posix())
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("reviewed FFmpeg evidence is unreadable") from exc
    technical = evidence.get("technical_license_assessment", {})
    if not isinstance(technical, dict) or technical.get("legal_approval") != "NOT_A_LEGAL_APPROVAL":
        raise RuntimeError("reviewed FFmpeg evidence does not preserve the legal-approval boundary")
    if (
        technical.get("gpl_components_present") is not False
        or technical.get("libx264_present") is not False
        or technical.get("h264_mf_present") is not True
    ):
        raise RuntimeError("reviewed FFmpeg evidence fails the LGPL/h264_mf packaging contract")
    def probe(*args: str) -> str:
        completed = subprocess.run(
            [str(binary), *args], capture_output=True, text=True, check=False,
            timeout=20,
        )
        return (completed.stdout + completed.stderr).strip() + "\n"
    version_text = probe("-version")
    buildconf_text = probe("-buildconf")
    license_text = probe("-L")
    (target / "FFMPEG_BINARY_VERSION.txt").write_text(version_text, encoding="utf-8")
    (target / "FFMPEG_BINARY_BUILDCONF.txt").write_text(buildconf_text, encoding="utf-8")
    (target / "FFMPEG_BINARY_LICENSE_OUTPUT.txt").write_text(license_text, encoding="utf-8")
    review = (
        "The exact binary below is bundled from this package's pinned FFmpeg payload.\n"
        "This is technical redistribution/licensing evidence, not a legal approval.\n"
        "Release owner review remains required before external redistribution.\n\n"
        + version_text
    )
    (target / "REDISTRIBUTION_REVIEW_REQUIRED.txt").write_text(review, encoding="utf-8")
    return {
        "status": "PINNED_LGPL_H264_MF_BINARY_RECORDED_LEGAL_REVIEW_REQUIRED",
        "binary": binary.relative_to(root).as_posix(),
        "ffprobe": ffprobe.relative_to(root).as_posix(),
        "distribution_evidence": evidence.get("distribution"),
        "technical_license_assessment": technical,
        "version_file": (target / "FFMPEG_BINARY_VERSION.txt").relative_to(root).as_posix(),
        "buildconf_file": (target / "FFMPEG_BINARY_BUILDCONF.txt").relative_to(root).as_posix(),
        "license_output_file": (target / "FFMPEG_BINARY_LICENSE_OUTPUT.txt").relative_to(root).as_posix(),
        "license_files": license_files,
    }


def collect_license_materials(package_root: Path, *, lock_path: Path) -> Path:
    root = Path(package_root).resolve()
    destination = root / "licenses"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    runtime_names = [item["name"] for item in locked_runtime_components(lock_path)]
    records = []
    for name in sorted(set(runtime_names), key=str.casefold):
        records.append(_copy_distribution_materials(root, str(name), destination / "python"))
    for name in BUILD_TOOLS:
        records.append(_copy_distribution_materials(root, name, destination / "build-tools"))
    ffmpeg = _collect_ffmpeg(root, destination)
    manifest = {
        "runtime_components": records,
        "ffmpeg": ffmpeg,
        "python_executable": sys.executable,
        "legal_status": "REQUIRES_RELEASE_OWNER_REVIEW",
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "AIDrama Studio third-party license materials",
        "",
        "These files were copied from the exact distributions present in the dedicated build environment.",
        "They are included for attribution and review; they do not alter any upstream license terms.",
        "",
    ]
    for record in records:
        lines.append(
            f"- {record.get('name')} {record.get('version', '')}: {record.get('status')}"
        )
        for filename in record.get("files", []):
            lines.append(f"  - {filename}")
    lines.extend(
        [
            "",
            f"FFmpeg: {ffmpeg.get('status')}",
            "FFmpeg redistribution remains subject to exact-binary release-owner review.",
        ]
    )
    (root / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root / "THIRD_PARTY_NOTICES.txt"


__all__ = ["collect_license_materials"]
