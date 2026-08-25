"""Dependency-free release metadata and distribution safety checks.

The module is intentionally usable before optional packaging tools are
installed.  It derives a CycloneDX SBOM from the committed ``uv.lock``, hashes
physical package files, records build provenance, and rejects known-unlicensed
font assets.  It never installs packages and never reads product credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence
from urllib.parse import quote

from aidrama_studio.branding import BRAND
from aidrama_studio.storage.migrations import MIGRATIONS


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = PROJECT_ROOT / "uv.lock"
FORBIDDEN_DISTRIBUTION_FONTS = frozenset(
    {
        "microsoftyaheibold.ttc",
        "microsoftyaheinormal.ttc",
        "stheitilight.ttc",
        "stheitimedium.ttc",
    }
)


class ReleaseDefinitionError(RuntimeError):
    """A release tree or provenance input is incomplete or unsafe."""


def _timestamp(value: str | None = None) -> str:
    return value or datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    target = Path(path)
    if not target.is_file():
        raise ReleaseDefinitionError(f"release artifact is not a file: {target.name}")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, content: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return target


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def locked_runtime_components(lock_path: Path = DEFAULT_LOCK) -> list[dict[str, object]]:
    """Return the runtime dependency closure rooted at the product package."""

    raw = Path(lock_path).read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    packages = data.get("package")
    if not isinstance(packages, list):
        raise ReleaseDefinitionError("uv.lock has no package inventory")
    by_name: dict[str, list[Mapping[str, object]]] = {}
    for item in packages:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip().lower()
        if name:
            by_name.setdefault(name, []).append(item)
    roots = by_name.get("moneyprinterturbo", [])
    if len(roots) != 1:
        raise ReleaseDefinitionError("uv.lock must contain one product root")
    pending = [
        str(item.get("name") or "").strip().lower()
        for item in roots[0].get("dependencies", [])
        if isinstance(item, Mapping)
    ]
    selected: dict[tuple[str, str], Mapping[str, object]] = {}
    visited: set[str] = set()
    while pending:
        name = pending.pop()
        if not name or name in visited:
            continue
        visited.add(name)
        candidates = by_name.get(name, [])
        for item in candidates:
            version = str(item.get("version") or "").strip()
            if not version:
                continue
            selected[(name, version)] = item
            for dependency in item.get("dependencies", []):
                if isinstance(dependency, Mapping):
                    pending.append(str(dependency.get("name") or "").strip().lower())
    components: list[dict[str, object]] = []
    for (name, version), item in sorted(selected.items()):
        source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
        registry = str(source.get("registry") or "")
        component: dict[str, object] = {
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{quote(name)}@{quote(version)}",
        }
        if registry:
            component["externalReferences"] = [
                {"type": "distribution", "url": registry}
            ]
        components.append(component)
    if not components:
        raise ReleaseDefinitionError("uv.lock runtime dependency closure is empty")
    return components


def write_sbom(
    output_path: Path,
    *,
    lock_path: Path = DEFAULT_LOCK,
    timestamp: str | None = None,
) -> Path:
    lock_sha = sha256_file(lock_path)
    components = locked_runtime_components(lock_path)
    serial = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://aidrama.local/sbom/{BRAND.version}/{lock_sha}",
    )
    value = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": _timestamp(timestamp),
            "component": {
                "type": "application",
                "name": BRAND.product_name,
                "version": BRAND.version,
            },
            "properties": [
                {"name": "aidrama:uv-lock-sha256", "value": lock_sha},
                {"name": "aidrama:dependency-scope", "value": "runtime-lock-closure"},
            ],
        },
        "components": components,
    }
    return _atomic_write(output_path, _canonical_json(value))


def audit_distribution_tree(package_root: Path) -> dict[str, object]:
    root = Path(package_root).resolve()
    if not root.is_dir():
        raise ReleaseDefinitionError("distribution tree does not exist")
    forbidden: list[str] = []
    ffmpeg: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        lowered = path.name.casefold()
        if lowered in FORBIDDEN_DISTRIBUTION_FONTS:
            forbidden.append(relative)
        if lowered.startswith("ffmpeg") and lowered.endswith(".exe"):
            ffmpeg.append(relative)
    if forbidden:
        raise ReleaseDefinitionError(
            "distribution contains fonts without a release-approved redistribution record: "
            + ", ".join(sorted(forbidden))
        )
    return {
        "forbidden_font_count": 0,
        "ffmpeg_binaries": sorted(ffmpeg),
        "ffmpeg_distribution_status": (
            "REQUIRES_EXACT_BINARY_LICENSE_AUDIT" if ffmpeg else "NOT_BUNDLED"
        ),
    }


def write_package_checksums(package_root: Path, output_path: Path) -> Path:
    root = Path(package_root).resolve()
    if not root.is_dir():
        raise ReleaseDefinitionError("distribution tree does not exist")
    release_root = (root / "release").resolve()
    entries: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved == Path(output_path).resolve() or release_root in resolved.parents:
            continue
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if relative.is_absolute() or ".." in relative.parts:
            raise ReleaseDefinitionError("unsafe package-relative path")
        entries.append((relative.as_posix(), sha256_file(path)))
    if not entries:
        raise ReleaseDefinitionError("distribution tree has no package files")
    content = "".join(f"{digest}  {name}\n" for name, digest in sorted(entries))
    return _atomic_write(output_path, content)


def current_git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseDefinitionError("git commit provenance is unavailable") from exc
    value = completed.stdout.strip().lower()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ReleaseDefinitionError("git commit provenance is invalid")
    return value


def write_build_provenance(
    output_path: Path,
    *,
    package_root: Path,
    sbom_path: Path,
    checksum_path: Path,
    git_commit: str | None = None,
    timestamp: str | None = None,
) -> Path:
    commit = (git_commit or current_git_commit()).strip().lower()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ReleaseDefinitionError("git commit provenance is invalid")
    root = Path(package_root).resolve()
    sbom = Path(sbom_path).resolve()
    checksums = Path(checksum_path).resolve()
    for item in (sbom, checksums):
        if not item.is_file() or root not in item.parents:
            raise ReleaseDefinitionError("release metadata must stay inside package root")
    audit = audit_distribution_tree(root)
    value = {
        "product_name": BRAND.product_name,
        "product_version": BRAND.version,
        "git_commit": commit,
        "schema_migration_version": max(version for version, _ in MIGRATIONS),
        "build_timestamp": _timestamp(timestamp),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "sbom": sbom.relative_to(root).as_posix(),
        "sbom_sha256": sha256_file(sbom),
        "package_checksums": checksums.relative_to(root).as_posix(),
        "package_checksums_sha256": sha256_file(checksums),
        "distribution_audit": audit,
    }
    return _atomic_write(output_path, _canonical_json(value))


def write_package_release_metadata(
    package_root: Path,
    *,
    lock_path: Path = DEFAULT_LOCK,
    git_commit: str | None = None,
    timestamp: str | None = None,
) -> tuple[Path, Path, Path]:
    root = Path(package_root).resolve()
    audit_distribution_tree(root)
    release = root / "release"
    sbom = write_sbom(release / "sbom.cdx.json", lock_path=lock_path, timestamp=timestamp)
    checksums = write_package_checksums(root, release / "package-files.sha256")
    provenance = write_build_provenance(
        release / "build-provenance.json",
        package_root=root,
        sbom_path=sbom,
        checksum_path=checksums,
        git_commit=git_commit,
        timestamp=timestamp,
    )
    return sbom, checksums, provenance


def write_artifact_checksums(artifacts: Iterable[Path], output_path: Path) -> Path:
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in artifacts:
        path = Path(raw)
        name = path.name
        if not name or name in seen:
            raise ReleaseDefinitionError("release artifact names must be unique")
        seen.add(name)
        entries.append((name, sha256_file(path)))
    if not entries:
        raise ReleaseDefinitionError("no distributable artifacts supplied")
    return _atomic_write(
        output_path,
        "".join(f"{digest}  {name}\n" for name, digest in sorted(entries)),
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate AIDrama release metadata")
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--git-commit")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        paths = write_package_release_metadata(
            args.package_root,
            lock_path=args.lock,
            git_commit=args.git_commit,
        )
    except ReleaseDefinitionError as exc:
        print(f"AIDrama release metadata unavailable: {exc}")
        return 2
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FORBIDDEN_DISTRIBUTION_FONTS",
    "ReleaseDefinitionError",
    "audit_distribution_tree",
    "locked_runtime_components",
    "sha256_file",
    "write_artifact_checksums",
    "write_build_provenance",
    "write_package_checksums",
    "write_package_release_metadata",
    "write_sbom",
]
