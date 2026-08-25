from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aidrama_studio.branding import BRAND
from desktop.release import (
    ReleaseDefinitionError,
    audit_distribution_tree,
    locked_runtime_components,
    write_artifact_checksums,
    write_package_release_metadata,
)


LOCK = """version = 1
revision = 1
requires-python = \">=3.11\"

[[package]]
name = \"moneyprinterturbo\"
version = \"1.3.4\"
source = { virtual = \".\" }
dependencies = [{ name = \"alpha\" }]

[[package]]
name = \"alpha\"
version = \"1.2.3\"
source = { registry = \"https://pypi.org/simple\" }
dependencies = [{ name = \"beta\" }]

[[package]]
name = \"beta\"
version = \"4.5.6\"
source = { registry = \"https://pypi.org/simple\" }

[[package]]
name = \"dev-only\"
version = \"9.9.9\"
source = { registry = \"https://pypi.org/simple\" }
"""


def test_release_metadata_is_lock_scoped_hashed_and_path_safe(tmp_path: Path):
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK, encoding="utf-8")
    package = tmp_path / "AIDramaStudio"
    package.mkdir()
    executable = package / "AIDramaStudio.exe"
    executable.write_bytes(b"desktop-binary")
    (package / "LICENSE").write_text("MIT", encoding="utf-8")

    sbom, checksums, provenance = write_package_release_metadata(
        package,
        lock_path=lock,
        git_commit="a" * 40,
        timestamp="2026-08-25T00:00:00+00:00",
    )

    components = locked_runtime_components(lock)
    assert [(item["name"], item["version"]) for item in components] == [
        ("alpha", "1.2.3"),
        ("beta", "4.5.6"),
    ]
    bom = json.loads(sbom.read_text(encoding="utf-8"))
    assert bom["bomFormat"] == "CycloneDX"
    assert bom["metadata"]["component"] == {
        "type": "application",
        "name": "AIDrama Studio",
        "version": "1.0.0",
    }
    checksum_text = checksums.read_text(encoding="utf-8")
    assert hashlib.sha256(b"desktop-binary").hexdigest() in checksum_text
    assert "AIDramaStudio.exe" in checksum_text
    build = json.loads(provenance.read_text(encoding="utf-8"))
    assert build["product_version"] == BRAND.version == "1.0.0"
    assert build["git_commit"] == "a" * 40
    assert build["schema_migration_version"] == 27
    assert build["sbom"] == "release/sbom.cdx.json"
    serialized = json.dumps(build)
    assert str(tmp_path) not in serialized
    assert build["distribution_audit"]["ffmpeg_distribution_status"] == "NOT_BUNDLED"


def test_distribution_audit_rejects_unapproved_proprietary_font(tmp_path: Path):
    package = tmp_path / "package"
    font = package / "resource" / "fonts" / "MicrosoftYaHeiNormal.ttc"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"font")

    with pytest.raises(ReleaseDefinitionError, match="fonts"):
        audit_distribution_tree(package)


def test_distribution_audit_flags_exact_ffmpeg_binary_for_separate_review(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "ffmpeg-win-x86_64-v7.1.exe").write_bytes(b"binary")

    audit = audit_distribution_tree(package)
    assert audit["ffmpeg_binaries"] == ["ffmpeg-win-x86_64-v7.1.exe"]
    assert audit["ffmpeg_distribution_status"] == "REQUIRES_EXACT_BINARY_LICENSE_AUDIT"


def test_final_distributable_checksum_is_streamed_and_named(tmp_path: Path):
    installer = tmp_path / "AIDramaStudio-1.0.0-Windows-x64-Setup.exe"
    installer.write_bytes(b"installer")
    output = write_artifact_checksums([installer], tmp_path / "SHA256SUMS")
    assert output.read_text(encoding="utf-8") == (
        hashlib.sha256(b"installer").hexdigest()
        + "  AIDramaStudio-1.0.0-Windows-x64-Setup.exe\n"
    )


def test_installer_definition_preserves_separate_user_data_directory():
    source = Path("installer/AIDramaStudio.iss").read_text(encoding="utf-8")
    assert "AppId=" in source
    assert "DefaultDirName={localappdata}\\Programs\\AIDrama Studio" in source
    assert "Source: \"..\\dist\\AIDramaStudio\\*\"" in source
    assert "[UninstallDelete]" not in source
    assert "%LOCALAPPDATA%\\AIDrama Studio" in source
