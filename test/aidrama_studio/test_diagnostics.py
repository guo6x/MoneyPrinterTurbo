from __future__ import annotations

import json
import hashlib
import os
import time
from pathlib import Path

from aidrama_studio.domain import ProviderTask, SourceKind, SourcePackItem
from aidrama_studio.services import DiagnosticsService, DiskSpaceService, ProjectService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


def _repo(root: Path) -> ProjectRepository:
    return ProjectRepository(DatabasePaths(root / "db" / "aidrama.db", root / "projects", root / "archived"))


def test_diagnostics_redacts_data_root_and_reports_disk(tmp_path: Path):
    repo = _repo(tmp_path)
    project = ProjectService(repo).create("Diagnostics")
    root = repo.project_directory(project.id)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / ".stale.in-progress.mp4"
    temporary.write_bytes(b"partial")
    old = time.time() - 3600
    os.utime(temporary, (old, old))
    report = DiagnosticsService(repo).scan(project.id)
    assert report["sqlite_integrity"] == "ok"
    assert report["schema_version"] >= 22
    assert report["projects"][0]["orphan_temporary_files"] == [".stale.in-progress.mp4"]
    assert str(tmp_path) not in json.dumps(report, ensure_ascii=False)


def test_cleanup_only_removes_temporary_files(tmp_path: Path):
    repo = _repo(tmp_path)
    project = ProjectService(repo).create("Cleanup")
    root = repo.project_directory(project.id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "canonical.mp4").write_bytes(b"keep")
    normal = root / "notes.tmp.backup.txt"
    normal.write_bytes(b"normal user file")
    temporary = root / ".render.in-progress.mp4"
    temporary.write_bytes(b"remove")
    old = time.time() - 3600
    os.utime(temporary, (old, old))
    removed = DiagnosticsService(repo).cleanup_safe_temporary_files(project.id)
    assert removed == [".render.in-progress.mp4"]
    assert (root / "canonical.mp4").is_file()
    assert normal.is_file()


def test_cleanup_preserves_fresh_and_canonical_referenced_temporary_files(tmp_path: Path):
    repo = _repo(tmp_path)
    project = ProjectService(repo).create("Provenance cleanup")
    root = repo.project_directory(project.id)
    root.mkdir(parents=True, exist_ok=True)
    fresh = root / ".fresh.partial"
    fresh.write_bytes(b"fresh")
    referenced = root / ".canonical.download"
    referenced.write_bytes(b"canonical")
    old = time.time() - 3600
    os.utime(referenced, (old, old))
    payload = referenced.read_bytes()
    repo.create_source_pack_item(SourcePackItem(
        id="source-temp", project_id=project.id, source_kind=SourceKind.DOCUMENT,
        display_filename="source.bin", mime_type="application/octet-stream",
        size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
        storage_path=referenced.name, created_at="2026-01-01T00:00:00+00:00",
    ))

    removed = DiagnosticsService(repo, stale_after_seconds=60).cleanup_safe_temporary_files(project.id)

    assert removed == []
    assert fresh.is_file() and referenced.is_file()


def test_scan_reports_source_missing_and_hash_mismatch_and_foreign_keys(tmp_path: Path):
    repo = _repo(tmp_path)
    project = ProjectService(repo).create("Integrity")
    root = repo.project_directory(project.id)
    root.mkdir(parents=True, exist_ok=True)
    bad = root / "bad.bin"
    bad.write_bytes(b"actual")
    for identifier, relative in (("source-missing", "missing.bin"), ("source-bad", "bad.bin")):
        repo.create_source_pack_item(SourcePackItem(
            id=identifier, project_id=project.id, source_kind=SourceKind.DOCUMENT,
            display_filename=relative, mime_type="application/octet-stream", size_bytes=1,
            sha256=hashlib.sha256(b"expected").hexdigest(), storage_path=relative,
            created_at="2026-01-01T00:00:00+00:00",
        ))

    report = DiagnosticsService(repo).scan(project.id)
    source = report["projects"][0]["canonical_media_integrity"]["source"]
    assert source["missing"] == ["source-missing"]
    assert source["hash_mismatches"] == ["source-bad"]
    assert report["sqlite_foreign_key_violations"] == []
    assert isinstance(report["ffmpeg_readiness"]["ready"], bool)


def test_cleanup_skips_project_with_active_provider_task(tmp_path: Path):
    repo = _repo(tmp_path)
    project = ProjectService(repo).create("Active")
    root = repo.project_directory(project.id)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / ".provider.partial"
    temporary.write_bytes(b"active")
    old = time.time() - 3600
    os.utime(temporary, (old, old))
    repo.create_provider_task(ProviderTask(
        id="provider-active", project_id=project.id, capability="VIDEO",
        provider_id="provider", model_id="model", idempotency_key="key",
        state="RUNNING", created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    ))

    assert DiagnosticsService(repo, stale_after_seconds=60).cleanup_safe_temporary_files(project.id) == []
    assert temporary.is_file()


def test_cleanup_rechecks_active_work_under_write_lock(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    project = ProjectService(repo).create("Cleanup race")
    temporary = repo.project_directory(project.id) / ".race.partial"
    temporary.write_bytes(b"active")
    old = time.time() - 3600
    os.utime(temporary, (old, old))
    original_transaction = repo.transaction
    injected = False

    def transaction_with_racing_task():
        nonlocal injected
        if not injected:
            injected = True
            repo.create_provider_task(ProviderTask(
                id="provider-race", project_id=project.id, capability="VIDEO",
                provider_id="provider", model_id="model", idempotency_key="race-key",
                state="RUNNING", created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ))
        return original_transaction()

    monkeypatch.setattr(repo, "transaction", transaction_with_racing_task)
    removed = DiagnosticsService(repo, stale_after_seconds=60).cleanup_safe_temporary_files(project.id)

    assert removed == []
    assert temporary.is_file()


def test_export_report_uses_central_secret_and_path_redaction(tmp_path: Path):
    repo = _repo(tmp_path)
    project = ProjectService(repo).create("Redacted export")
    service = DiagnosticsService(repo)
    report = service.scan(project.id)
    report["operator"] = {
        "Authorization": "Bearer should-not-survive",
        "message": "api_key=should-not-survive C:\\Users\\secret\\file.txt",
        "signed_url": "https://cdn.example/file?signature=secret",
    }
    service.scan = lambda _project_id=None: report
    destination = tmp_path / "diagnostics.json"

    service.export_report(destination, project_id=project.id)

    payload = destination.read_text(encoding="utf-8")
    assert "should-not-survive" not in payload
    assert "C:\\Users\\secret" not in payload
    assert "signed_url" not in payload


def test_disk_preflight_is_truthful_for_unavailable_space(tmp_path: Path):
    repo = _repo(tmp_path)
    project = ProjectService(repo).create("Disk")
    result = DiskSpaceService(repo).preflight(10**18, project_id=project.id, reserve_bytes=0)
    assert result["ready"] is False
    assert result["reason"] == "可用空间不足"
