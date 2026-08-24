"""Deterministic quality-control checks for generated production artifacts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from aidrama_studio.domain import (
    ProductionArtifact,
    ProductionQCMetric,
    ProductionQCMetricStatus,
    ProductionQCResult,
    ProductionQCStatus,
    ProductionReview,
    ProductionReviewDecision,
)
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ProductionQCServiceError(RuntimeError):
    """Raised when a QC operation crosses a project or lifecycle boundary."""


class ProductionQCService:
    """Run reproducible file/metadata/traceability checks and persist reports."""

    SUPPORTED_MEDIA_TYPES = {
        "video/mp4", "video/webm", "video/quicktime", "video/x-matroska",
        "audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg",
        "image/jpeg", "image/png", "image/webp",
    }
    SUFFIX_MEDIA_TYPES = {
        ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    }

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def run_qc(
        self,
        project_id: str,
        execution_id: str,
        artifact_id: str | None = None,
    ) -> ProductionQCResult:
        execution = self._get_execution(project_id, execution_id)
        artifact = self._select_artifact(execution_id, artifact_id)
        result = self.repository.create_production_qc_result(
            ProductionQCResult(
                id=uuid4().hex,
                project_id=project_id,
                execution_id=execution_id,
                artifact_id=artifact.id if artifact else None,
                status=ProductionQCStatus.QC_PENDING,
                created_at=_now(),
            )
        )
        started_at = _now()
        result = self.repository.update_production_qc_result(
            result.id, status=ProductionQCStatus.QC_RUNNING, started_at=started_at
        )
        try:
            checks = self._run_checks(project_id, execution, artifact)
            for check in checks:
                self.repository.create_production_qc_metric(
                    ProductionQCMetric(
                        id=uuid4().hex,
                        result_id=result.id,
                        metric_name=check["metric_name"],
                        category=check["category"],
                        status=check["status"],
                        value_json=check.get("value", {}),
                        message=check.get("message", ""),
                        created_at=_now(),
                    )
                )
            failed = any(check["status"] is ProductionQCMetricStatus.FAIL for check in checks)
            status = ProductionQCStatus.QC_FAILED if failed else ProductionQCStatus.QC_PASS
            summary = self._summary(checks)
            report = self._report(
                result_id=result.id,
                project_id=project_id,
                execution=execution,
                artifact=artifact,
                status=status,
                summary=summary,
                checks=checks,
                generated_at=_now(),
            )
            report_path = self._write_report(project_id, execution_id, result.id, report)
            return self.repository.update_production_qc_result(
                result.id,
                status=status,
                report_path=report_path,
                summary_json=summary,
                finished_at=_now(),
            )
        except Exception as exc:
            # QC failures remain queryable and are never silently discarded.
            metric = {
                "metric_name": "qc_internal_error",
                "category": "SYSTEM",
                "status": ProductionQCMetricStatus.FAIL,
                "value": {},
                "message": str(exc),
            }
            self.repository.create_production_qc_metric(
                ProductionQCMetric(
                    id=uuid4().hex, result_id=result.id, metric_name=metric["metric_name"],
                    category=metric["category"], status=metric["status"],
                    value_json={}, message=metric["message"], created_at=_now(),
                )
            )
            summary = {"total": 1, "passed": 0, "failed": 1, "skipped": 0}
            return self.repository.update_production_qc_result(
                result.id,
                status=ProductionQCStatus.QC_FAILED,
                summary_json=summary | {"error": str(exc)},
                finished_at=_now(),
            )

    run = run_qc
    retry_qc = run_qc

    def get_result(self, project_id: str, result_id: str) -> ProductionQCResult:
        self._require_project(project_id)
        result = self.repository.get_production_qc_result(result_id)
        if result is None or result.project_id != project_id:
            raise ProductionQCServiceError("ProductionQCResult 不属于该项目")
        return result

    def list_results(self, project_id: str, execution_id: str | None = None) -> list[ProductionQCResult]:
        self._require_project(project_id)
        if execution_id is not None:
            self._get_execution(project_id, execution_id)
        return self.repository.list_production_qc_results(project_id, execution_id)

    list_qc_results = list_results

    def list_metrics(self, project_id: str, result_id: str) -> list[ProductionQCMetric]:
        self.get_result(project_id, result_id)
        return self.repository.list_production_qc_metrics(result_id)

    def create_review(
        self,
        project_id: str,
        result_id: str,
        decision: ProductionReviewDecision | str,
        *,
        reviewer: str = "system",
        notes: str = "",
    ) -> ProductionReview:
        result = self.get_result(project_id, result_id)
        if isinstance(decision, str):
            try:
                decision = ProductionReviewDecision(decision)
            except ValueError as exc:
                raise ProductionQCServiceError("review decision 无效") from exc
        return self.repository.create_production_review(
            ProductionReview(
                id=uuid4().hex, project_id=project_id, qc_result_id=result.id,
                decision=decision, reviewer=reviewer or "system", notes=notes, created_at=_now(),
            )
        )

    def list_reviews(self, project_id: str, result_id: str | None = None) -> list[ProductionReview]:
        self._require_project(project_id)
        if result_id is not None:
            self.get_result(project_id, result_id)
        return self.repository.list_production_reviews(project_id, result_id)

    def _run_checks(self, project_id: str, execution, artifact: ProductionArtifact | None) -> list[dict[str, object]]:
        checks: list[dict[str, object]] = []
        if artifact is None:
            return [self._check("artifact_exists", "FILE", False, {}, "artifact 不存在")]

        artifact_path = self._resolve_artifact_path(project_id, artifact.path)
        exists = artifact_path.is_file()
        checks.append(self._check("artifact_exists", "FILE", exists, {"path": artifact.path}, "artifact 文件存在" if exists else "artifact 文件不存在"))
        size = artifact_path.stat().st_size if exists else 0
        checks.append(self._check("artifact_size", "FILE", size > 0, {"size_bytes": size}, "artifact 非空" if size > 0 else "artifact 为空"))

        metadata = artifact.metadata_json or {}
        media_type = self._media_type(artifact, metadata)
        supported = media_type in self.SUPPORTED_MEDIA_TYPES
        checks.append(self._check("supported_media_type", "FILE", supported, {"mime_type": media_type or ""}, "媒体类型受支持" if supported else "媒体类型不受支持"))

        is_video = media_type.startswith("video/") or Path(artifact.path).suffix.lower() in {".mp4", ".webm", ".mov", ".mkv"}
        if is_video:
            checks.extend(self._video_checks(metadata))
            checks.extend(self._visual_checks(metadata))
            checks.append(self._audio_check(metadata))
        else:
            checks.extend([
                self._skipped("video_duration", "VIDEO", "非视频 artifact，跳过"),
                self._skipped("video_resolution", "VIDEO", "非视频 artifact，跳过"),
                self._skipped("video_codec", "VIDEO", "非视频 artifact，跳过"),
                self._skipped("black_frame", "VISUAL", "非视频 artifact，跳过"),
                self._skipped("static_frame", "VISUAL", "非视频 artifact，跳过"),
                self._skipped("audio_stream", "AUDIO", "非视频 artifact，跳过"),
            ])
        checks.append(self._traceability_check(execution, artifact, metadata))
        return checks

    def _video_checks(self, metadata: Mapping[str, object]) -> list[dict[str, object]]:
        duration = metadata.get("duration_seconds", metadata.get("duration"))
        duration_ok = isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0
        resolution = self._resolution(metadata)
        resolution_ok = resolution is not None and resolution[0] > 0 and resolution[1] > 0
        codec = metadata.get("codec") or metadata.get("video_codec") or metadata.get("codec_name")
        codec_ok = isinstance(codec, str) and bool(codec.strip())
        return [
            self._check("video_duration", "VIDEO", duration_ok, {"duration_seconds": duration}, "视频时长有效" if duration_ok else "视频时长 metadata 无效"),
            self._check("video_resolution", "VIDEO", resolution_ok, {"resolution": f"{resolution[0]}x{resolution[1]}" if resolution else ""}, "分辨率有效" if resolution_ok else "视频分辨率 metadata 无效"),
            self._check("video_codec", "VIDEO", codec_ok, {"codec": codec or ""}, "codec metadata 有效" if codec_ok else "codec metadata 缺失"),
        ]

    def _visual_checks(self, metadata: Mapping[str, object]) -> list[dict[str, object]]:
        black = self._flagged(metadata, "black_frame_detected", "black_frame", "black_frames", "black_frame_ratio")
        static = self._flagged(metadata, "static_frame_detected", "static_frame", "static_frames", "static_frame_ratio")
        motion_score = metadata.get("motion_score")
        if isinstance(motion_score, (int, float)) and not isinstance(motion_score, bool) and motion_score <= 0:
            static = True
        return [
            self._check("black_frame", "VISUAL", not black, {"detected": black}, "未检测到黑帧" if not black else "检测到黑帧"),
            self._check("static_frame", "VISUAL", not static, {"detected": static}, "未检测到静帧" if not static else "检测到静帧"),
        ]

    @staticmethod
    def _audio_check(metadata: Mapping[str, object]) -> dict[str, object]:
        value = metadata.get("audio_stream", metadata.get("has_audio", metadata.get("audio_streams")))
        passed = value is True or (isinstance(value, Mapping) and bool(value.get("present"))) or (
            isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        )
        return ProductionQCService._check("audio_stream", "AUDIO", passed, {"present": bool(value) if value is not None else False}, "audio stream 存在" if passed else "audio stream 缺失")

    @staticmethod
    def _traceability_check(execution, artifact: ProductionArtifact, metadata: Mapping[str, object]) -> dict[str, object]:
        expected_shots = list(execution.input_snapshot.shot_parameters) if execution.input_snapshot else []
        expected_refs = execution.input_snapshot.reference_asset_versions if execution.input_snapshot else {}
        execution_ok = metadata.get("execution_id") == execution.id
        shot_id = metadata.get("shot_id")
        shot_ok = isinstance(shot_id, str) and shot_id in expected_shots
        references = metadata.get("reference_versions", metadata.get("reference_asset_versions"))
        refs_ok = isinstance(references, Mapping) and all(references.get(key) == value for key, value in expected_refs.items())
        artifact_relation_ok = artifact.execution_id == execution.id
        passed = execution_ok and shot_ok and refs_ok and artifact_relation_ok
        return ProductionQCService._check(
            "traceability", "TRACEABILITY", passed,
            {"execution_id": metadata.get("execution_id"), "shot_id": shot_id, "reference_versions": dict(references) if isinstance(references, Mapping) else {}},
            "execution/shot/reference traceability 有效" if passed else "execution/shot/reference traceability 无效",
        )

    @staticmethod
    def _check(metric_name: str, category: str, passed: bool, value: dict[str, object], message: str) -> dict[str, object]:
        return {"metric_name": metric_name, "category": category, "status": ProductionQCMetricStatus.PASS if passed else ProductionQCMetricStatus.FAIL, "value": value, "message": message}

    @staticmethod
    def _skipped(metric_name: str, category: str, message: str) -> dict[str, object]:
        return {"metric_name": metric_name, "category": category, "status": ProductionQCMetricStatus.SKIPPED, "value": {}, "message": message}

    @staticmethod
    def _summary(checks: list[dict[str, object]]) -> dict[str, int]:
        return {
            "total": len(checks),
            "passed": sum(item["status"] is ProductionQCMetricStatus.PASS for item in checks),
            "failed": sum(item["status"] is ProductionQCMetricStatus.FAIL for item in checks),
            "skipped": sum(item["status"] is ProductionQCMetricStatus.SKIPPED for item in checks),
        }

    def _select_artifact(self, execution_id: str, artifact_id: str | None) -> ProductionArtifact | None:
        artifacts = self.repository.list_production_artifacts(execution_id)
        if artifact_id is not None:
            artifact = self.repository.get_production_artifact(artifact_id)
            if artifact is None or artifact.execution_id != execution_id:
                raise ProductionQCServiceError("artifact 不属于该 execution")
            return artifact
        return artifacts[0] if artifacts else None

    def _get_execution(self, project_id: str, execution_id: str):
        self._require_project(project_id)
        execution = self.repository.get_production_execution(execution_id)
        if execution is None:
            raise ProductionQCServiceError("ProductionExecution 不存在")
        job = self.repository.get_production_job(execution.production_job_id)
        if job is None or job.project_id != project_id:
            raise ProductionQCServiceError("ProductionExecution 不属于该项目")
        return execution

    def _require_project(self, project_id: str):
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProductionQCServiceError(f"项目不存在: {project_id}")
        return project

    def _resolve_artifact_path(self, project_id: str, relative_path: str) -> Path:
        self._require_project(project_id)
        if not isinstance(relative_path, str) or not relative_path.strip() or "\x00" in relative_path:
            raise ProductionQCServiceError("artifact path 无效")
        normalized = relative_path.replace("\\", "/")
        if normalized.startswith("/") or PureWindowsPath(relative_path).drive:
            raise ProductionQCServiceError("artifact path 必须是相对路径")
        parts = PurePosixPath(normalized).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ProductionQCServiceError("artifact path 不能越过项目目录")
        root = (self.repository.paths.projects / project_id).resolve()
        target = (root / Path(*parts)).resolve()
        if root not in target.parents:
            raise ProductionQCServiceError("artifact path 不属于该项目")
        return target

    def _write_report(self, project_id: str, execution_id: str, result_id: str, report: dict[str, object]) -> str:
        root = (self.repository.paths.projects / project_id).resolve()
        qc_root = (root / "production" / execution_id / "qc").resolve()
        if root not in qc_root.parents:
            raise ProductionQCServiceError("QC report path escapes project root")
        qc_root.mkdir(parents=True, exist_ok=True)
        base = qc_root / "qc_report.json"
        target = base if not base.exists() else qc_root / f"qc_report-{result_id}.json"
        with target.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
        return PurePosixPath("production", execution_id, "qc", target.name).as_posix()

    @staticmethod
    def _report(**kwargs) -> dict[str, object]:
        checks = kwargs["checks"]
        return {
            "qc_result_id": kwargs["result_id"], "project_id": kwargs["project_id"],
            "execution_id": kwargs["execution"].id, "artifact_id": kwargs["artifact"].id if kwargs["artifact"] else None,
            "status": kwargs["status"].value, "summary": kwargs["summary"],
            "metrics": [
                {"name": item["metric_name"], "category": item["category"], "status": item["status"].value, "value": item["value"], "message": item["message"]}
                for item in checks
            ],
            "traceability": {
                "execution_id": kwargs["execution"].id,
                "shot_ids": list(kwargs["execution"].input_snapshot.shot_parameters) if kwargs["execution"].input_snapshot else [],
                "reference_versions": dict(kwargs["execution"].input_snapshot.reference_asset_versions) if kwargs["execution"].input_snapshot else {},
            },
            "generated_at": kwargs["generated_at"],
        }

    @staticmethod
    def _media_type(artifact: ProductionArtifact, metadata: Mapping[str, object]) -> str:
        value = metadata.get("mime_type") or metadata.get("media_type")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return ProductionQCService.SUFFIX_MEDIA_TYPES.get(Path(artifact.path).suffix.lower(), "")

    @staticmethod
    def _resolution(metadata: Mapping[str, object]) -> tuple[int, int] | None:
        raw = metadata.get("resolution")
        if isinstance(raw, Mapping):
            width, height = raw.get("width"), raw.get("height")
        elif isinstance(raw, str):
            match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", raw)
            if not match:
                return None
            width, height = match.groups()
        else:
            width, height = metadata.get("width"), metadata.get("height")
        if isinstance(width, bool) or isinstance(height, bool):
            return None
        try:
            return int(width), int(height)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _flagged(metadata: Mapping[str, object], *keys: str) -> bool:
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, bool):
                if value:
                    return True
            elif isinstance(value, (int, float)) and value > 0:
                return True
            elif isinstance(value, str) and value.strip().lower() in {"true", "yes", "detected", "black", "static"}:
                return True
        return False
