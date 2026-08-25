from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from aidrama_studio.pages import dashboard


class _ArchiveStub:
    MAX_ARCHIVE_BYTES = 1024 * 1024

    def __init__(self, root: Path):
        self.repository = SimpleNamespace(
            paths=SimpleNamespace(archived_projects=root / "archives")
        )
        self.exported = None
        self.verified = None
        self.imported = None

    def export_project(self, project_id, destination):
        self.exported = (project_id, Path(destination))
        Path(destination).write_bytes(b"verified-aidrama")

    def verify_importable(self, archive_path):
        assert Path(archive_path).read_bytes() == b"uploaded-aidrama"
        self.verified = Path(archive_path)

    def import_project(self, archive_path, *, project_id=None):
        assert self.verified == Path(archive_path)
        self.imported = (Path(archive_path), project_id)
        return "project-1" if project_id is None else project_id


def test_dashboard_archive_helpers_use_public_verified_service_boundary(tmp_path: Path):
    service = _ArchiveStub(tmp_path)
    project = SimpleNamespace(id="project-1", title="Project")

    exported = dashboard._export_archive_path(service, project)
    assert exported.read_bytes() == b"verified-aidrama"
    restored = dashboard._import_archive_stream(
        service, io.BytesIO(b"uploaded-aidrama")
    )

    assert service.exported[0] == "project-1"
    assert service.verified is not None
    assert service.imported[1] is None
    assert restored == "project-1"


def test_dashboard_exposes_archive_restore_and_verified_delete_recovery():
    source = Path(dashboard.__file__).read_text(encoding="utf-8")
    for label in (
        "导出 / 导入 / 恢复项目 (.aidrama)",
        "生成已验证的 .aidrama",
        "验证并恢复为新项目",
        "Verified Recovery Archive",
        "下载 Recovery Archive (.aidrama)",
    ):
        assert label in source
    assert "ProjectArchiveService(service.repository)" in source
    assert "service.delete(project.id, confirmed=True)" in source
    assert dashboard._archive_download_name("../My Project") == "My_Project.aidrama"
