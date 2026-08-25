"""Deterministic, metadata-only Final Assembly manifest service.

Task010A stops at freezing qualified source identities.  This module never
opens a renderer and never copies video bytes; it snapshots canonical
ProductionJob/Shot/Execution/Artifact/QC/Review relationships into an
append-only manifest.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from aidrama_studio.domain import (
    FinalAssembly,
    FinalAssemblyItem,
    FinalAssemblyManifest,
    FinalAssemblyReadiness,
    FinalAssemblySource,
    FinalAssemblyStatus,
    ProductionQCStatus,
    ProductionReviewDecision,
    ProductionShotSourceDecision,
    ProductionShotSourceDecisionType,
    ProductionShotSourceSelectionKind,
    ProductionExecutionStatus,
)
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class FinalAssemblyServiceError(RuntimeError):
    """Raised when manifest creation crosses a production or path boundary."""


class FinalAssemblyService:
    """Project-scoped readiness, source selection, and manifest freezing."""

    SUPPORTED_VIDEO_TYPES = {
        "video",
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/x-matroska",
        "mp4",
        "webm",
        "mov",
        "mkv",
        "rendered_video",
    }
    SUPPORTED_VIDEO_MIME_TYPES = {
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/x-matroska",
    }

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def calculate_readiness(self, project_id: str, production_job_id: str) -> FinalAssemblyReadiness:
        """Derive readiness from current canonical production records."""
        job = self._get_job(project_id, production_job_id)
        shots = self._ordered_shots(job.id)
        blocked_reasons: list[str] = []
        eligible = 0
        estimated_duration = 0.0
        for shot in shots:
            source, reason = self._select_source_for_shot(project_id, job.id, shot, strict=False)
            if source is None:
                blocked_reasons.append(f"{shot.shot_id}: {reason}")
                continue
            eligible += 1
            estimated_duration += source.estimated_duration
        return FinalAssemblyReadiness(
            total_shots=len(shots),
            eligible_shots=eligible,
            blocked_shots=len(shots) - eligible,
            estimated_duration=round(estimated_duration, 6),
            blocked_reasons=blocked_reasons,
        )

    def select_qualified_source(
        self,
        project_id: str,
        production_job_id: str,
        production_shot_id: str,
    ) -> FinalAssemblySource | None:
        """Select one qualified source using persisted attempt ordering.

        Candidate order is the repository's canonical ``created_at,rowid``
        execution/artifact/QC order; filesystem timestamps and filenames are
        never consulted.  A later APPROVED/ACCEPTED review takes precedence
        over otherwise-qualified candidates, otherwise the most recent
        qualified persisted execution is selected.
        """
        job = self._get_job(project_id, production_job_id)
        shot = self._resolve_shot(job.id, production_shot_id)
        source, reason = self._select_source_for_shot(project_id, job.id, shot, strict=True)
        if source is None:
            raise FinalAssemblyServiceError(reason)
        return source

    def select_shot_source(
        self,
        project_id: str,
        production_job_id: str,
        production_shot_id: str,
        *,
        production_execution_id: str,
        production_artifact_id: str,
        selected_by: str = "user",
        notes: str = "",
        promote_preview: bool = False,
    ) -> ProductionShotSourceDecision:
        """Append an explicit human choice over one currently qualified source."""
        if not isinstance(selected_by, str) or not selected_by.strip():
            raise FinalAssemblyServiceError("selected_by 不能为空")
        job = self._get_job(project_id, production_job_id)
        shot = self._resolve_shot(job.id, production_shot_id)
        source, reason = self._select_source_for_shot(
            project_id,
            job.id,
            shot,
            strict=True,
            ignore_explicit=True,
            required_source=(production_execution_id, production_artifact_id),
            allow_preview=promote_preview,
        )
        if source is None:
            raise FinalAssemblyServiceError(reason)
        execution = self.repository.get_production_execution(
            source.production_execution_id
        )
        if execution is None:
            raise FinalAssemblyServiceError("selected execution 不存在")
        brief = (
            self.repository.get_generation_brief(execution.generation_brief_id)
            if execution.generation_brief_id
            else None
        )
        history = self.repository.list_production_shot_source_decisions(
            project_id, shot.id
        )
        decision = ProductionShotSourceDecision(
            id=uuid4().hex,
            project_id=project_id,
            production_job_id=job.id,
            production_shot_id=shot.id,
            sequence_number=len(history) + 1,
            decision_type=ProductionShotSourceDecisionType.SELECTED,
            selection_kind=(
                ProductionShotSourceSelectionKind.PREVIEW_PROMOTED
                if promote_preview
                else ProductionShotSourceSelectionKind.FINAL_ACCEPTED
            ),
            production_execution_id=source.production_execution_id,
            production_artifact_id=source.production_artifact_id,
            qc_result_id=source.qc_result_id,
            review_id=source.review_id,
            generation_brief_id=execution.generation_brief_id,
            generation_brief_sha256=brief.sha256 if brief is not None else None,
            selected_by=selected_by.strip(),
            notes=notes,
            created_at=_now(),
        )
        try:
            return self.repository.create_production_shot_source_decision(decision)
        except (KeyError, ValueError) as exc:
            raise FinalAssemblyServiceError(str(exc)) from exc

    def release_shot_source(
        self,
        project_id: str,
        production_job_id: str,
        production_shot_id: str,
        *,
        selected_by: str = "user",
        notes: str = "",
    ) -> ProductionShotSourceDecision:
        job = self._get_job(project_id, production_job_id)
        shot = self._resolve_shot(job.id, production_shot_id)
        history = self.repository.list_production_shot_source_decisions(
            project_id, shot.id
        )
        if (
            not history
            or history[-1].decision_type
            is not ProductionShotSourceDecisionType.SELECTED
        ):
            raise FinalAssemblyServiceError("该 Shot 当前没有显式 source selection")
        selected = history[-1]
        released = selected.model_copy(
            update={
                "id": uuid4().hex,
                "sequence_number": selected.sequence_number + 1,
                "decision_type": ProductionShotSourceDecisionType.RELEASED,
                "selected_by": selected_by,
                "notes": notes,
                "created_at": _now(),
            }
        )
        try:
            return self.repository.create_production_shot_source_decision(released)
        except (KeyError, ValueError) as exc:
            raise FinalAssemblyServiceError(str(exc)) from exc

    def create_assembly(
        self,
        project_id: str,
        production_job_id: str,
        *,
        assembly_id: str | None = None,
        freeze: bool = False,
    ) -> FinalAssembly:
        """Create a new DRAFT assembly; optionally freeze it immediately."""
        job = self._get_job(project_id, production_job_id)
        now = _now()
        profile = self.repository.get_output_profile(job.output_profile_id) if job.output_profile_id else None
        profile_hash = hashlib.sha256(json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest() if profile is not None else None
        assembly = self.repository.create_final_assembly(
            FinalAssembly(
                id=assembly_id or uuid4().hex,
                project_id=project_id,
                production_job_id=job.id,
                output_profile_id=job.output_profile_id,
                output_profile_hash=profile_hash,
                status=FinalAssemblyStatus.DRAFT,
                created_at=now,
                updated_at=now,
            )
        )
        return self.freeze_manifest(project_id, assembly.id) if freeze else assembly

    def freeze_manifest(self, project_id: str, assembly_id: str) -> FinalAssembly:
        """Freeze the current qualified sources into an immutable READY set."""
        if not isinstance(assembly_id, str) and hasattr(assembly_id, "id"):
            assembly_id = str(assembly_id.id)
        assembly = self._get_assembly(project_id, assembly_id)
        if assembly.status is FinalAssemblyStatus.READY:
            return assembly
        if assembly.status is not FinalAssemblyStatus.DRAFT:
            raise FinalAssemblyServiceError(
                f"只有 DRAFT FinalAssembly 可以 freeze，当前状态为 {assembly.status.value}"
            )

        readiness = self.calculate_readiness(project_id, assembly.production_job_id)
        if not readiness.ready:
            reasons = "; ".join(readiness.blocked_reasons) or "没有可用于 Final Assembly 的镜头"
            raise FinalAssemblyServiceError(f"Final Assembly 尚未 READY: {reasons}")

        existing_items = self.repository.list_final_assembly_items(assembly.id)
        if existing_items:
            raise FinalAssemblyServiceError("DRAFT FinalAssembly 已包含 item，不能重新构建")

        job = self._get_job(project_id, assembly.production_job_id)
        # Resolve every source before writing any item.  This prevents a
        # transient file disappearance or concurrent production retry from
        # leaving a partially populated DRAFT manifest.
        selected_sources = [
            (shot, self.select_qualified_source(project_id, job.id, shot.id))
            for shot in self._ordered_shots(job.id)
        ]
        timeline = 0.0
        items = []
        for shot, source in selected_sources:
            duration = float(source.source_duration_seconds if source.source_duration_seconds is not None else source.estimated_duration)
            start = timeline
            end = start + max(0.0, duration)
            timeline = end
            items.append(FinalAssemblyItem(
                id=uuid4().hex,
                final_assembly_id=assembly.id,
                # Preserve the canonical ProductionShot order exactly;
                # production shots are persisted 1-based by ProductionService.
                order_index=shot.order_index,
                production_shot_id=shot.id,
                production_execution_id=source.production_execution_id,
                production_artifact_id=source.production_artifact_id,
                qc_result_id=source.qc_result_id,
                review_id=source.review_id,
                source_decision_id=source.source_decision_id,
                source_path=source.source_path,
                created_at=_now(),
                source_sha256=source.source_sha256,
                source_duration_seconds=duration,
                timeline_start_seconds=round(start, 6),
                timeline_end_seconds=round(end, 6),
                trimmed_duration_seconds=duration,
            ))
        try:
            return self.repository.freeze_final_assembly_atomic(
                assembly.id, items, updated_at=_now()
            )
        except (KeyError, ValueError) as exc:
            raise FinalAssemblyServiceError(str(exc)) from exc

    freeze = freeze_manifest
    create_manifest = create_assembly
    readiness = calculate_readiness

    def get_manifest(self, project_id: str, assembly_id: str) -> FinalAssemblyManifest:
        if not isinstance(assembly_id, str) and hasattr(assembly_id, "id"):
            assembly_id = str(assembly_id.id)
        assembly = self._get_assembly(project_id, assembly_id)
        return FinalAssemblyManifest.from_assembly(
            assembly,
            self.repository.list_final_assembly_items(assembly.id),
        )

    def list_assemblies(
        self,
        project_id: str,
        production_job_id: str | None = None,
    ) -> list[FinalAssembly]:
        self._require_project(project_id)
        if production_job_id is not None:
            self._get_job(project_id, production_job_id)
        return self.repository.list_final_assemblies(project_id, production_job_id)

    get = get_manifest

    def _select_source_for_shot(
        self,
        project_id: str,
        job_id: str,
        shot,
        *,
        strict: bool,
        ignore_explicit: bool = False,
        required_source: tuple[str, str] | None = None,
        allow_preview: bool = False,
    ):
        executions = self.repository.list_production_executions(job_id)
        candidates: list[tuple[tuple[int, int, int], bool, FinalAssemblySource]] = []
        last_reason = "没有找到 qualified source"
        decisions = self.repository.list_production_shot_source_decisions(
            project_id, shot.id
        )
        current_decision = decisions[-1] if decisions else None
        current_brief = self.repository.get_selected_generation_brief(
            project_id, job_id, shot.shot_id
        )
        for execution_index, execution in enumerate(executions):
            if execution.status is not ProductionExecutionStatus.SUCCEEDED:
                last_reason = f"没有 SUCCEEDED execution（当前 {execution.status.value}）"
                continue
            if (
                current_brief is not None
                and execution.generation_brief_id != current_brief.id
            ):
                last_reason = "execution 使用的 GenerationBrief 已不是当前显式版本"
                continue
            artifacts = self.repository.list_production_artifacts(execution.id)
            if not self._execution_contains_shot(execution, shot.shot_id, shot.id, artifacts):
                continue
            if not artifacts:
                last_reason = "execution 没有 artifact"
                continue
            qc_results = self.repository.list_production_qc_results(project_id, execution.id)
            for artifact_index, artifact in enumerate(artifacts):
                if not self._is_supported_video_artifact(artifact):
                    last_reason = "artifact 不是受支持的视频 artifact"
                    continue
                if artifact.metadata_json.get("artifact_role") == "PREVIEW":
                    explicitly_promoted = bool(
                        current_decision is not None
                        and current_decision.decision_type
                        is ProductionShotSourceDecisionType.SELECTED
                        and current_decision.selection_kind
                        is ProductionShotSourceSelectionKind.PREVIEW_PROMOTED
                        and current_decision.production_execution_id == execution.id
                        and current_decision.production_artifact_id == artifact.id
                    )
                    requested_promotion = bool(
                        allow_preview
                        and required_source == (execution.id, artifact.id)
                    )
                    if not explicitly_promoted and not requested_promotion:
                        last_reason = "Preview artifact 未经显式提升，不能作为最终成片来源"
                        continue
                try:
                    source_path = self._validate_source_path(project_id, artifact.path)
                except FinalAssemblyServiceError as exc:
                    last_reason = str(exc)
                    continue
                if not source_path.is_file():
                    last_reason = "artifact source 文件不存在"
                    continue
                actual_sha = self._sha256(source_path)
                expected_sha = artifact.metadata_json.get("sha256")
                if isinstance(expected_sha, str) and expected_sha and actual_sha != expected_sha:
                    last_reason = "artifact SHA256 与持久化 metadata 不一致"
                    continue
                matching_results = [item for item in qc_results if item.artifact_id == artifact.id]
                if not matching_results:
                    last_reason = "artifact 没有 QC result"
                    continue
                # The newest persisted QC result is authoritative for the
                # artifact, matching ProductionOrchestrator's QC semantics.
                qc_result = matching_results[-1]
                if qc_result.status is not ProductionQCStatus.QC_PASS:
                    last_reason = f"QC result 不是 QC_PASS（当前 {qc_result.status.value}）"
                    continue
                reviews = self.repository.list_production_reviews(
                    project_id, qc_result.id
                )
                # Reviews are append-only.  The latest decision for this QC
                # result is authoritative; an old rejection remains readable
                # history but must not block a later explicit approval.
                latest_review = reviews[-1] if reviews else None
                latest_decision = self._review_decision(latest_review) if latest_review else ""
                if latest_decision == "REJECTED":
                    last_reason = "human review rejected source"
                    continue
                accepted_review = latest_review if latest_decision in {"APPROVED", "ACCEPTED"} else None
                selected_review = latest_review
                source = FinalAssemblySource(
                    production_shot_id=shot.id,
                    production_execution_id=execution.id,
                    production_artifact_id=artifact.id,
                    qc_result_id=qc_result.id,
                    review_id=selected_review.id if selected_review else None,
                    source_path=artifact.path.replace("\\", "/"),
                    estimated_duration=self._duration(artifact.metadata_json, execution, shot),
                    source_sha256=actual_sha,
                    source_duration_seconds=self._duration(artifact.metadata_json, execution, shot),
                )
                candidates.append(
                    ((execution_index, artifact_index, qc_results.index(qc_result)), accepted_review is not None, source)
                )
        if not candidates:
            return (None, last_reason)
        if required_source is not None:
            exact = [
                candidate
                for candidate in candidates
                if (
                    candidate[2].production_execution_id,
                    candidate[2].production_artifact_id,
                )
                == required_source
            ]
            if not exact:
                return (None, "指定的 shot source 当前不满足 qualification")
            return max(exact, key=lambda candidate: candidate[0])[2], ""
        if not ignore_explicit:
            if (
                decisions
                and decisions[-1].decision_type
                is ProductionShotSourceDecisionType.SELECTED
            ):
                selected_decision = decisions[-1]
                explicit = [
                    candidate
                    for candidate in candidates
                    if candidate[2].production_execution_id
                    == selected_decision.production_execution_id
                    and candidate[2].production_artifact_id
                    == selected_decision.production_artifact_id
                    and candidate[2].qc_result_id == selected_decision.qc_result_id
                ]
                if not explicit:
                    return (
                        None,
                        "显式选择的 shot source 已不满足当前 qualification；请重新选择",
                    )
                if current_brief is not None and (
                    selected_decision.generation_brief_id != current_brief.id
                    or selected_decision.generation_brief_sha256
                    != current_brief.sha256
                ):
                    return (None, "显式选择的 shot source creative provenance 已过期")
                source = max(explicit, key=lambda candidate: candidate[0])[2]
                return source.model_copy(
                    update={"source_decision_id": selected_decision.id}
                ), ""
        accepted_candidates = [candidate for candidate in candidates if candidate[1]]
        selected = max(accepted_candidates or candidates, key=lambda candidate: candidate[0])
        return selected[2], ""

    def _get_assembly(self, project_id: str, assembly_id: str) -> FinalAssembly:
        self._require_project(project_id)
        assembly = self.repository.get_final_assembly(assembly_id)
        if assembly is None or assembly.project_id != project_id:
            raise FinalAssemblyServiceError("FinalAssembly 不属于该项目")
        return assembly

    def _get_job(self, project_id: str, job_id: str):
        self._require_project(project_id)
        job = self.repository.get_production_job(job_id)
        if job is None or job.project_id != project_id:
            raise FinalAssemblyServiceError("ProductionJob 不属于该项目")
        return job

    def _resolve_shot(self, job_id: str, shot_id: str):
        shots = self.repository.list_production_shots(job_id)
        shot = next((item for item in shots if item.id == shot_id or item.shot_id == shot_id), None)
        if shot is None:
            raise FinalAssemblyServiceError("ProductionShot 不属于该 ProductionJob")
        return shot

    def _ordered_shots(self, job_id: str):
        return sorted(
            self.repository.list_production_shots(job_id),
            key=lambda shot: (shot.order_index, shot.id),
        )

    def _require_project(self, project_id: str):
        if self.repository.get_project(project_id) is None:
            raise FinalAssemblyServiceError(f"项目不存在: {project_id}")

    @staticmethod
    def _execution_contains_shot(execution, shot_id: str, production_shot_id: str, artifacts=None) -> bool:
        snapshot = execution.input_snapshot
        if snapshot is not None and (
            shot_id in snapshot.shot_parameters or production_shot_id in snapshot.shot_parameters
        ):
            return True
        for artifact in artifacts or ():
            metadata = artifact.metadata_json or {}
            if metadata.get("shot_id") in {shot_id, production_shot_id}:
                return True
        return False

    @classmethod
    def _is_supported_video_artifact(cls, artifact) -> bool:
        artifact_type = str(artifact.artifact_type or "").strip().lower()
        metadata = artifact.metadata_json or {}
        mime = str(metadata.get("mime_type") or metadata.get("media_type") or "").strip().lower()
        return (
            artifact_type in cls.SUPPORTED_VIDEO_TYPES
            or artifact_type.startswith("video/")
            or mime in cls.SUPPORTED_VIDEO_MIME_TYPES
            or mime.startswith("video/")
            or Path(artifact.path).suffix.lower() in {".mp4", ".webm", ".mov", ".mkv"}
        )

    def _validate_source_path(self, project_id: str, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip() or "\x00" in relative_path:
            raise FinalAssemblyServiceError("source path 无效")
        normalized = relative_path.strip().replace("\\", "/")
        if normalized.startswith("/") or PureWindowsPath(relative_path).drive:
            raise FinalAssemblyServiceError("source path 必须是项目相对路径")
        parts = PurePosixPath(normalized).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise FinalAssemblyServiceError("source path 不能越过项目目录")
        root = (self.repository.paths.projects / project_id).resolve()
        target = (root / Path(*parts)).resolve()
        if root not in target.parents:
            raise FinalAssemblyServiceError("source path 不属于该项目")
        return target

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _duration(metadata: Mapping[str, object] | None, execution=None, shot=None) -> float:
        metadata = metadata or {}
        value = metadata.get("duration_seconds", metadata.get("duration"))
        if value is None and execution is not None and shot is not None and execution.input_snapshot:
            parameters = execution.input_snapshot.shot_parameters.get(shot.shot_id, {})
            if isinstance(parameters, Mapping):
                value = parameters.get("duration_seconds", parameters.get("duration", 0))
        if value is None:
            value = 0
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return 0.0
        return float(value)

    @staticmethod
    def _review_decision(review) -> str:
        value = getattr(review.decision, "value", review.decision)
        return str(value).strip().upper()


__all__ = ["FinalAssemblyService", "FinalAssemblyServiceError"]
