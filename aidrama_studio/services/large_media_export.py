"""Large-file-safe delivery copy for canonical project media."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath

from aidrama_studio.storage.repositories import ProjectRepository


class LargeMediaExportError(RuntimeError):
    pass


class LargeMediaExportCancelled(LargeMediaExportError):
    pass


class LargeMediaExportService:
    """Copy a canonical MP4 without loading it into Python memory."""

    CHUNK_SIZE = 2 * 1024 * 1024

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def copy(
        self,
        project_id: str,
        *,
        source_relative_path: str,
        source_sha256: str,
        source_size_bytes: int,
        destination: Path,
        operation_id: str,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        source = self._source(project_id, source_relative_path)
        expected_size = int(source_size_bytes)
        if expected_size <= 0 or source.stat().st_size != expected_size:
            raise LargeMediaExportError("成片源文件大小与冻结 provenance 不一致")
        expected_hash = str(source_sha256).strip().lower()
        if len(expected_hash) != 64 or any(
            character not in "0123456789abcdef" for character in expected_hash
        ):
            raise LargeMediaExportError("成片源文件 SHA256 provenance 无效")
        destination = Path(destination).expanduser()
        if not destination.is_absolute() or destination.suffix.lower() != ".mp4":
            raise LargeMediaExportError("导出目标必须是绝对 MP4 路径")
        if destination.exists():
            raise LargeMediaExportError("导出目标已存在；不会覆盖现有文件")
        parent = destination.parent
        if not parent.is_dir():
            raise LargeMediaExportError("导出目标目录不存在")
        partial = destination.with_name(
            f".{destination.name}.{operation_id}.partial"
        )
        if partial.exists():
            raise LargeMediaExportError("同一导出任务的 partial 文件已存在")

        digest = hashlib.sha256()
        copied = 0
        try:
            with source.open("rb") as source_handle, partial.open("xb") as target_handle:
                while True:
                    if cancelled is not None and cancelled():
                        raise LargeMediaExportCancelled("导出已安全取消")
                    chunk = source_handle.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    target_handle.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                    if progress is not None:
                        progress(copied, expected_size)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            if copied != expected_size or digest.hexdigest() != expected_hash:
                raise LargeMediaExportError("导出副本校验失败")
            # A hard link is an atomic no-overwrite promotion on the same
            # filesystem. The partial name is then removed, leaving one
            # independent delivery directory entry. Canonical source remains.
            os.link(partial, destination)
            partial.unlink()
            return {
                "destination_name": destination.name,
                "size_bytes": copied,
                "sha256": digest.hexdigest(),
                "canonical_source_preserved": True,
            }
        except FileExistsError as exc:
            raise LargeMediaExportError(
                "导出目标已存在；不会覆盖现有文件"
            ) from exc
        finally:
            if partial.exists():
                try:
                    partial.unlink()
                except OSError:
                    pass

    def _source(self, project_id: str, relative_path: str) -> Path:
        if self.repository.get_project(project_id) is None:
            raise LargeMediaExportError("项目不存在")
        normalized = str(relative_path or "").strip().replace("\\", "/")
        if (
            not normalized
            or normalized.startswith("/")
            or PureWindowsPath(relative_path).drive
        ):
            raise LargeMediaExportError("成片源必须是项目相对路径")
        parts = PurePosixPath(normalized).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise LargeMediaExportError("成片源路径不能越过项目目录")
        root = (self.repository.paths.projects / project_id).resolve()
        source = (root / Path(*parts)).resolve()
        if root not in source.parents or source.suffix.lower() != ".mp4":
            raise LargeMediaExportError("成片源不属于该项目")
        if not source.is_file() or source.stat().st_size <= 0:
            raise LargeMediaExportError("成片源文件不存在")
        return source


__all__ = [
    "LargeMediaExportCancelled",
    "LargeMediaExportError",
    "LargeMediaExportService",
]
