from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from aidrama_studio.services import ProjectArchiveError, ProjectArchiveService, ProjectService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


def _repo(root: Path) -> ProjectRepository:
    return ProjectRepository(DatabasePaths(root / "db" / "aidrama.db", root / "projects", root / "archived"))


def _rewrite_manifest(archive_path: Path, mutate, *, recompute_hash: bool = True) -> None:
    with zipfile.ZipFile(archive_path) as source:
        entries = [(info.filename, source.read(info)) for info in source.infolist()]
    manifest = json.loads(next(data for name, data in entries if name == "manifest.json"))
    mutate(manifest)
    if recompute_hash:
        manifest["content_sha256"] = ProjectArchiveService._content_sha256(manifest)
    replacement = archive_path.with_suffix(".replacement")
    with zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, data in entries:
            target.writestr(
                name,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
                if name == "manifest.json"
                else data,
            )
    replacement.replace(archive_path)


def _insert_story(repository: ProjectRepository, project_id: str, revision_id: str) -> None:
    with repository.transaction() as connection:
        connection.execute(
            "INSERT INTO story_bible_revisions"
            "(id,project_id,version,status,content_json,generation_input_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (revision_id, project_id, 1, "DRAFT", "{}", None, "now", "now"),
        )


def _insert_provider_task(repository: ProjectRepository, project_id: str, state: str) -> None:
    with repository.transaction() as connection:
        connection.execute(
            "INSERT INTO provider_tasks"
            "(id,project_id,execution_id,capability,provider_id,model_id,idempotency_key,"
            "provider_task_id,state,request_summary_json,metadata_json,submitted_at,last_polled_at,"
            "next_poll_at,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"task-{state}", project_id, None, "VIDEO_GENERATIVE", "fake", "fake-model",
                f"key-{state}", None, state, "{}", "{}", None, None, None, None, "now", "now",
            ),
        )


def test_project_export_import_round_trip_and_collision_guard(tmp_path: Path):
    source = _repo(tmp_path / "source")
    description = "正常创意文本：token、authorization、secret 都可以是角色台词。"
    project = ProjectService(source).create("Archive project", description=description)
    (source.project_directory(project.id) / "note.txt").write_text("hello", encoding="utf-8")
    archive = ProjectArchiveService(source).export_project(project.id, tmp_path / "project.zip")

    target = _repo(tmp_path / "target")
    imported = ProjectArchiveService(target).import_project(archive)
    assert target.get_project(imported).title == "Archive project"
    assert target.get_project(imported).description == description
    assert (target.paths.projects / imported / "note.txt").read_text(encoding="utf-8") == "hello"
    with pytest.raises(ProjectArchiveError, match="已存在"):
        ProjectArchiveService(target).import_project(archive)


def test_project_import_rejects_zip_slip_and_symlink_entries(tmp_path: Path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escape", b"bad")
    target = _repo(tmp_path / "target")
    with pytest.raises(ProjectArchiveError, match="路径穿越"):
        ProjectArchiveService(target).import_project(archive)


def test_export_rejects_symlink_project_root(tmp_path: Path, monkeypatch):
    repository = _repo(tmp_path / "source")
    project = ProjectService(repository).create("Symlink root")
    project_root = repository.project_directory(project.id)
    original = Path.is_symlink

    def report_project_root_as_symlink(path: Path) -> bool:
        return path == project_root or original(path)

    monkeypatch.setattr(Path, "is_symlink", report_project_root_as_symlink)
    with pytest.raises(ProjectArchiveError, match="symlink"):
        ProjectArchiveService(repository).export_project(
            project.id, tmp_path / "unsafe.aidrama"
        )


def test_export_uses_exact_allowlist_excludes_global_rows_and_includes_execution_graph(tmp_path: Path):
    source = _repo(tmp_path / "source")
    project = ProjectService(source).create("Graph")
    _insert_story(source, project.id, "story-graph")
    with source.transaction() as connection:
        connection.execute(
            "INSERT INTO structured_script_revisions VALUES (?,?,?,?,?,?,?,?,?)",
            ("script-graph", project.id, 1, "DRAFT", "story-graph", "{}", None, "now", "now"),
        )
        connection.execute(
            "INSERT INTO shot_plan_revisions VALUES (?,?,?,?,?,?,?,?,?)",
            ("shot-plan-graph", project.id, 1, "DRAFT", "script-graph", "{}", None, "now", "now"),
        )
        connection.execute(
            "INSERT INTO production_jobs(id,project_id,shot_plan_revision_id,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("job-graph", project.id, "shot-plan-graph", "READY", "now", "now"),
        )
        connection.execute(
            "INSERT INTO production_executions"
            "(id,production_job_id,status,worker_type,created_at,input_snapshot_json) VALUES (?,?,?,?,?,?)",
            ("execution-graph", "job-graph", "SUCCEEDED", "test", "now", "{}"),
        )
        connection.execute(
            "INSERT INTO production_events VALUES (?,?,?,?,?)",
            ("event-graph", "execution-graph", "FINISHED", "{}", "now"),
        )
        connection.execute(
            "INSERT INTO production_artifacts VALUES (?,?,?,?,?,?)",
            ("artifact-graph", "execution-graph", "video", "result.mp4", "{}", "now"),
        )
        connection.execute(
            "INSERT INTO provider_selection_settings VALUES (?,?,?,?,?,?)",
            ("global-selection", None, "CUSTOM", "{}", "now", "now"),
        )
        connection.execute(
            "INSERT INTO provider_selection_settings VALUES (?,?,?,?,?,?)",
            ("project-selection", project.id, "CUSTOM", "{}", "now", "now"),
        )

    archive = ProjectArchiveService(source).export_project(project.id, tmp_path / "graph.aidrama")
    with zipfile.ZipFile(archive) as handle:
        manifest = json.loads(handle.read("manifest.json"))
    assert set(manifest["tables"]) == set(ProjectArchiveService.TABLE_ALLOWLIST)
    assert [row["id"] for row in manifest["tables"]["production_executions"]] == ["execution-graph"]
    assert [row["id"] for row in manifest["tables"]["production_events"]] == ["event-graph"]
    assert [row["id"] for row in manifest["tables"]["production_artifacts"]] == ["artifact-graph"]
    assert [row["id"] for row in manifest["tables"]["provider_selection_settings"]] == ["project-selection"]

    target = _repo(tmp_path / "target")
    imported = ProjectArchiveService(target).import_project(archive)
    with target.transaction() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM production_executions WHERE id='execution-graph'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_selection_settings WHERE project_id=?", (imported,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_selection_settings WHERE project_id IS NULL"
        ).fetchone()[0] == 0


def test_export_rejects_operational_secrets_without_changing_creative_text(tmp_path: Path):
    source = _repo(tmp_path / "source")
    description = "角色说：token、authorization 和 secret 都是普通台词。"
    project = ProjectService(source).create("Safe archive", description=description)
    secret = "do-not-export-this-secret"
    with source.transaction() as connection:
        connection.execute(
            "INSERT INTO provider_tasks"
            "(id,project_id,execution_id,capability,provider_id,model_id,idempotency_key,"
            "provider_task_id,state,request_summary_json,metadata_json,submitted_at,last_polled_at,"
            "next_poll_at,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "secret-task", project.id, None, "VISION", "provider", "model", "safe-key",
                None, "FAILED",
                json.dumps({"nested": {"api_key": secret}, "signed_url": f"https://cdn.example/file?signature={secret}"}),
                json.dumps({"message": f"Authorization: Bearer {secret} C:\\Users\\private\\file.mp4"}),
                None, None, None, f"token={secret} C:\\Users\\private\\error.log", "now", "now",
            ),
        )

    destination = tmp_path / "safe.aidrama"
    with pytest.raises(ProjectArchiveError, match="敏感信息"):
        ProjectArchiveService(source).export_project(project.id, destination)
    assert not destination.exists()
    assert source.get_project(project.id).description == description


def test_import_rejects_duplicate_member_and_extra_file(tmp_path: Path):
    source = _repo(tmp_path / "source")
    project = ProjectService(source).create("Ambiguous ZIP")
    note = source.project_directory(project.id) / "note.txt"
    note.write_text("hello", encoding="utf-8")
    archive = ProjectArchiveService(source).export_project(project.id, tmp_path / "ambiguous.aidrama")
    with zipfile.ZipFile(archive, "a") as handle:
        handle.writestr("files/note.txt", b"replacement")
    with pytest.raises(ProjectArchiveError, match="重复"):
        ProjectArchiveService(_repo(tmp_path / "duplicate-target")).import_project(archive)

    extra = ProjectArchiveService(source).export_project(project.id, tmp_path / "extra.aidrama")
    with zipfile.ZipFile(extra, "a") as handle:
        handle.writestr("unexpected.bin", b"extra")
    with pytest.raises(ProjectArchiveError, match="额外"):
        ProjectArchiveService(_repo(tmp_path / "extra-target")).import_project(extra)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("schema_version", 999, "schema version"),
        ("product_version", "999.0.0", "product major version"),
    ),
)
def test_import_validates_schema_and_product_version(tmp_path: Path, field: str, value, message: str):
    source = _repo(tmp_path / "source")
    project = ProjectService(source).create("Versions")
    archive = ProjectArchiveService(source).export_project(project.id, tmp_path / f"{field}.aidrama")
    _rewrite_manifest(archive, lambda manifest: manifest.__setitem__(field, value))
    with pytest.raises(ProjectArchiveError, match=message):
        ProjectArchiveService(_repo(tmp_path / "target")).import_project(archive)


def test_v1_import_preserves_verified_preview_archive_compatibility(tmp_path: Path):
    source = _repo(tmp_path / "source")
    project = ProjectService(source).create("Preview archive")
    archive = ProjectArchiveService(source).export_project(
        project.id, tmp_path / "preview.aidrama"
    )
    _rewrite_manifest(
        archive,
        lambda manifest: manifest.__setitem__("product_version", "0.1.0"),
    )

    target = _repo(tmp_path / "target")
    imported = ProjectArchiveService(target).import_project(archive)
    assert imported == project.id


def test_import_rejects_manifest_and_file_tampering(tmp_path: Path):
    source = _repo(tmp_path / "source")
    project = ProjectService(source).create("Hashes")
    note = source.project_directory(project.id) / "note.txt"
    note.write_text("original", encoding="utf-8")
    archive = ProjectArchiveService(source).export_project(project.id, tmp_path / "manifest-tamper.aidrama")
    _rewrite_manifest(
        archive,
        lambda manifest: manifest["tables"]["projects"][0].__setitem__("title", "tampered"),
        recompute_hash=False,
    )
    with pytest.raises(ProjectArchiveError, match="content hash"):
        ProjectArchiveService(_repo(tmp_path / "manifest-target")).import_project(archive)

    file_archive = ProjectArchiveService(source).export_project(project.id, tmp_path / "file-tamper.aidrama")
    with zipfile.ZipFile(file_archive) as handle:
        entries = [(info.filename, handle.read(info)) for info in handle.infolist()]
    replacement = file_archive.with_suffix(".replacement")
    with zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name, data in entries:
            handle.writestr(name, b"tampered" if name == "files/note.txt" else data)
    replacement.replace(file_archive)
    target = _repo(tmp_path / "file-target")
    with pytest.raises(ProjectArchiveError, match="hash"):
        ProjectArchiveService(target).import_project(file_archive)
    assert target.get_project(project.id) is None
    assert not target.project_directory(project.id).exists()


def test_import_db_collision_rolls_back_project_row_and_restored_files(tmp_path: Path):
    source = _repo(tmp_path / "source")
    project = ProjectService(source).create("Rollback")
    _insert_story(source, project.id, "shared-revision")
    (source.project_directory(project.id) / "note.txt").write_text("restore me", encoding="utf-8")
    archive = ProjectArchiveService(source).export_project(project.id, tmp_path / "rollback.aidrama")

    target = _repo(tmp_path / "target")
    other = ProjectService(target).create("Other")
    _insert_story(target, other.id, "shared-revision")
    with pytest.raises(ProjectArchiveError, match="collision"):
        ProjectArchiveService(target).import_project(archive)
    assert target.get_project(project.id) is None
    assert not target.project_directory(project.id).exists()
    assert list(target.paths.projects.glob(".*.importing")) == []
    assert target.get_project(other.id) is not None


@pytest.mark.parametrize(
    "relative",
    ("files/name:stream", "files/CON", "files/aux.txt", "files/trailing. "),
)
def test_archive_paths_reject_windows_aliases(relative: str):
    with pytest.raises(ProjectArchiveError, match="相对路径"):
        ProjectArchiveService._safe_relative(relative)


def test_import_rejects_oversized_manifest_before_read(tmp_path: Path):
    source = _repo(tmp_path / "source")
    project = ProjectService(source).create("Manifest size")
    archive = ProjectArchiveService(source).export_project(
        project.id, tmp_path / "manifest.aidrama"
    )
    target = ProjectArchiveService(_repo(tmp_path / "target"))
    target.MAX_MANIFEST_BYTES = 1
    with pytest.raises(ProjectArchiveError, match="manifest 超过大小限制"):
        target.import_project(archive)


def test_delete_blocks_queued_production_job_without_provider_task(tmp_path: Path):
    repository = _repo(tmp_path / "source")
    project = ProjectService(repository).create("Queued")
    _insert_story(repository, project.id, "story-queued")
    with repository.transaction() as connection:
        connection.execute(
            "INSERT INTO structured_script_revisions VALUES (?,?,?,?,?,?,?,?,?)",
            ("script-queued", project.id, 1, "DRAFT", "story-queued", "{}", None, "now", "now"),
        )
        connection.execute(
            "INSERT INTO shot_plan_revisions VALUES (?,?,?,?,?,?,?,?,?)",
            ("shot-queued", project.id, 1, "DRAFT", "script-queued", "{}", None, "now", "now"),
        )
        connection.execute(
            "INSERT INTO production_jobs(id,project_id,shot_plan_revision_id,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("job-queued", project.id, "shot-queued", "QUEUED", "now", "now"),
        )

    with pytest.raises(ValueError, match="活动制作任务"):
        ProjectService(repository).delete(project.id, confirmed=True)
    assert repository.get_project(project.id) is not None


def test_delete_creates_verified_aidrama_backup_restorable_by_safe_import(tmp_path: Path):
    repository = _repo(tmp_path / "source")
    project = ProjectService(repository).create("Delete me", description="保留完整创意文本")
    (repository.project_directory(project.id) / "note.txt").write_text("recover", encoding="utf-8")

    result = ProjectService(repository).delete(project.id, confirmed=True)

    assert result.deleted is True
    assert result.recovery_archive_to is not None
    assert result.recovery_archive_to.suffix == ".aidrama"
    assert result.recovery_archive_to.is_file()
    assert repository.get_project(project.id) is None
    assert not repository.project_directory(project.id).exists()

    imported = ProjectArchiveService(repository).import_project(result.recovery_archive_to)
    assert imported == project.id
    assert repository.get_project(imported).description == "保留完整创意文本"
    assert (repository.project_directory(imported) / "note.txt").read_text(encoding="utf-8") == "recover"


def test_delete_blocks_every_unknown_provider_nonterminal_state(tmp_path: Path):
    repository = _repo(tmp_path / "source")
    project = ProjectService(repository).create("Busy")
    _insert_provider_task(repository, project.id, "PROVIDER_SUCCEEDED_ARTIFACT_PENDING")

    with pytest.raises(ValueError, match="活动制作任务"):
        ProjectService(repository).delete(project.id, confirmed=True)
    assert repository.get_project(project.id) is not None
    assert repository.project_directory(project.id).exists()


def test_delete_rechecks_provider_state_in_delete_transaction(tmp_path: Path, monkeypatch):
    repository = _repo(tmp_path / "source")
    project = ProjectService(repository).create("Race")
    marker = repository.project_directory(project.id) / "note.txt"
    marker.write_text("still here", encoding="utf-8")
    original_verify = ProjectArchiveService.verify_importable

    def inject_after_verification(service, archive_path):
        original_verify(service, archive_path)
        _insert_provider_task(repository, project.id, "RECONCILIATION_REQUIRED")

    monkeypatch.setattr(ProjectArchiveService, "verify_importable", inject_after_verification)
    with pytest.raises(ValueError, match="活动制作任务"):
        ProjectService(repository).delete(project.id, confirmed=True)
    assert repository.get_project(project.id) is not None
    assert marker.read_text(encoding="utf-8") == "still here"
