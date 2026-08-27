"""Read-only projection for the AIDrama Director Workspace.

The workspace composes canonical Script, Shot, Production, QC, Review,
Reference, Vision and Final Assembly records.  It does not persist a parallel
shot state or choose files by filesystem timestamps.  Candidate ordering is
the repository's durable ordering and the default preview is always the
qualified source selected by :class:`FinalAssemblyService` when one exists.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aidrama_studio.domain import (
    ProductionExecutionStatus,
    ProductionQCStatus,
    ProductionReviewDecision,
    ProductionShotStatus,
    ReferenceBindingType,
)
from aidrama_studio.storage.repositories import ProjectRepository

from .current_state import CurrentProductionStateService
from .final_assembly import FinalAssemblyService, FinalAssemblyServiceError
from .reference_assets import ReferenceAssetService, ReferenceAssetServiceError


ContinuityAdapter = Callable[..., Mapping[str, object] | None]


@dataclass(frozen=True, slots=True)
class TimelineSegment:
    shot_id: str
    order: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class TimelineGap:
    kind: str
    label: str
    start_seconds: float
    duration_seconds: float = 0.0
    missing_orders: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceReference:
    binding_kind: str
    binding_id: str
    label: str
    version_id: str
    version_number: int
    locked: bool
    provenance: str
    thumbnail_path: Path | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceQCMetric:
    name: str
    status: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceCandidate:
    candidate_id: str
    execution_id: str
    artifact_id: str
    label: str
    execution_status: str
    technical_qc_status: str
    technical_qc_metrics: tuple[WorkspaceQCMetric, ...]
    review_status: str
    vision_status: str
    vision_metrics: Mapping[str, object]
    is_selected_source: bool
    source_decision_id: str | None
    artifact_role: str
    preview_path: Path | None
    mime_type: str
    references: tuple[WorkspaceReference, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class WorkspaceBeat:
    beat_id: str
    beat_order: int
    scene_id: str
    scene_title: str
    text: str
    beat_type: str
    shot_ids: tuple[str, ...]
    mapping_kind: str


@dataclass(frozen=True, slots=True)
class WorkspaceContinuity:
    available: bool
    status: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceShot:
    shot_id: str
    production_shot_id: str | None
    number: int
    order: int
    duration_seconds: float
    timeline_start_seconds: float
    timeline_end_seconds: float
    scene_id: str
    scene_label: str
    character_ids: tuple[str, ...]
    character_labels: tuple[str, ...]
    beat_ids: tuple[str, ...]
    production_status: str
    qc_status: str
    review_status: str
    final_source_status: str
    workspace_state: str
    preview_candidate_id: str | None
    references: tuple[WorkspaceReference, ...]
    candidates: tuple[WorkspaceCandidate, ...]
    continuity: WorkspaceContinuity


@dataclass(frozen=True, slots=True)
class DirectorWorkspaceProjection:
    project_id: str
    script_revision_id: str | None
    shot_plan_revision_id: str | None
    production_job_id: str | None
    state: str
    shots: tuple[WorkspaceShot, ...] = ()
    beats: tuple[WorkspaceBeat, ...] = ()
    timeline: tuple[TimelineSegment, ...] = ()
    gaps: tuple[TimelineGap, ...] = ()
    total_duration_seconds: float = 0.0
    target_duration_seconds: float = 0.0
    vision_available: bool = True
    continuity_available: bool = False
    diagnostic: str = ""


def _value(item: object, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _enum_value(value: object, default: str = "") -> str:
    raw = getattr(value, "value", value)
    return str(raw if raw is not None else default).strip().upper()


def _duration(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def build_timeline(
    shots: Sequence[object], target_duration_seconds: float = 0.0
) -> tuple[tuple[TimelineSegment, ...], tuple[TimelineGap, ...], float]:
    """Build an ordered, contiguous read model from authored durations.

    Gaps are explicit projection warnings.  Missing order numbers do not
    invent time; target shortfall uses the project's formal target duration.
    """

    indexed = list(enumerate(shots))
    ordered = sorted(
        indexed,
        key=lambda pair: (
            int(_value(pair[1], "order", pair[0] + 1) or pair[0] + 1),
            pair[0],
        ),
    )
    segments: list[TimelineSegment] = []
    gaps: list[TimelineGap] = []
    cursor = 0.0
    expected_order = 1
    for fallback_index, shot in ordered:
        raw_order = _value(shot, "order", fallback_index + 1)
        try:
            order = int(raw_order)
        except (TypeError, ValueError):
            order = fallback_index + 1
        if order > expected_order:
            missing = tuple(range(expected_order, order))
            gaps.append(
                TimelineGap(
                    kind="ORDER_GAP",
                    label="缺少镜头序号 "
                    + ", ".join(f"{item:02d}" for item in missing),
                    start_seconds=round(cursor, 6),
                    missing_orders=missing,
                )
            )
        duration = _duration(_value(shot, "duration_seconds", 0))
        if duration <= 0:
            gaps.append(
                TimelineGap(
                    kind="INVALID_DURATION",
                    label=f"镜头 {order:02d} 缺少有效时长",
                    start_seconds=round(cursor, 6),
                )
            )
        shot_id = str(_value(shot, "id", f"shot-{fallback_index + 1}"))
        end = cursor + duration
        segments.append(
            TimelineSegment(
                shot_id=shot_id,
                order=order,
                start_seconds=round(cursor, 6),
                end_seconds=round(end, 6),
                duration_seconds=round(duration, 6),
            )
        )
        cursor = end
        expected_order = max(expected_order, order + 1)
    target = _duration(target_duration_seconds)
    if target > cursor:
        gaps.append(
            TimelineGap(
                kind="TARGET_SHORTFALL",
                label=f"距离目标时长还差 {target - cursor:g} 秒",
                start_seconds=round(cursor, 6),
                duration_seconds=round(target - cursor, 6),
            )
        )
    return tuple(segments), tuple(gaps), round(cursor, 6)


class DirectorWorkspaceProjectionService:
    """Compose formal domain records into one project-scoped UI read model."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        continuity_adapter: ContinuityAdapter | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.current_state = CurrentProductionStateService(self.repository)
        self.final_sources = FinalAssemblyService(self.repository)
        self.references = ReferenceAssetService(self.repository)
        self.continuity_adapter = continuity_adapter

    def project(self, project_id: str) -> DirectorWorkspaceProjection:
        project = self.repository.get_project(project_id)
        if project is None:
            raise ValueError("项目不存在")
        current = self.current_state.derive(project_id)
        plan_revision = self._plan_revision(project_id, current.current_job_id)
        if plan_revision is None:
            return DirectorWorkspaceProjection(
                project_id=project_id,
                script_revision_id=None,
                shot_plan_revision_id=None,
                production_job_id=current.current_job_id,
                state="EMPTY",
                target_duration_seconds=float(project.target_duration_seconds),
                continuity_available=self.continuity_adapter is not None,
            )
        script_revision = self.repository.get_script_revision(
            plan_revision["source_script_revision_id"]
        )
        if script_revision is None:
            return DirectorWorkspaceProjection(
                project_id=project_id,
                script_revision_id=None,
                shot_plan_revision_id=plan_revision["id"],
                production_job_id=current.current_job_id,
                state="BLOCKED",
                target_duration_seconds=float(project.target_duration_seconds),
                continuity_available=self.continuity_adapter is not None,
                diagnostic="Shot Plan 的正式 Script revision 不存在",
            )
        plan_shots = list(plan_revision["content"].shots)
        timeline, gaps, total = build_timeline(
            plan_shots, float(project.target_duration_seconds)
        )
        timeline_by_shot = {item.shot_id: item for item in timeline}
        script = script_revision["content"]
        story_revision = self.repository.get_story_revision(
            script_revision["source_story_revision_id"]
        )
        story = story_revision["content"] if story_revision else None
        scene_by_id = {scene.id: scene for scene in script.scenes}
        character_labels = {
            character.id: character.name
            for character in (story.characters if story else [])
        }
        location_labels = {
            location.id: location.name
            for location in (story.locations if story else [])
        }
        production_shots = {item.shot_id: item for item in current.shots}
        executions = (
            self.repository.list_production_executions(current.current_job_id)
            if current.current_job_id
            else []
        )
        projected_shots: list[WorkspaceShot] = []
        for shot in sorted(plan_shots, key=lambda item: (item.order, item.id)):
            production_shot = production_shots.get(shot.id)
            qualified = (
                current.qualified_sources.get(production_shot.id)
                if production_shot is not None
                else None
            )
            scene = scene_by_id.get(shot.scene_id)
            exact_binding_ids = self._shot_binding_ids(shot, scene)
            candidates = self._candidate_projection(
                project_id,
                shot,
                production_shot,
                executions,
                qualified,
                exact_binding_ids,
                character_labels,
                location_labels,
            )
            default_candidate = next(
                (item for item in candidates if item.is_selected_source),
                candidates[-1] if candidates else None,
            )
            references = (
                default_candidate.references
                if default_candidate is not None and default_candidate.references
                else self._current_references(
                    project_id,
                    exact_binding_ids,
                    character_labels,
                    location_labels,
                    story_revision["id"] if story_revision else None,
                )
            )
            qc_status = (
                default_candidate.technical_qc_status
                if default_candidate is not None
                else "NOT_STARTED"
            )
            review_status = (
                default_candidate.review_status
                if default_candidate is not None
                else "NOT_STARTED"
            )
            source_status = self._source_status(default_candidate)
            workspace_state = self._workspace_state(
                shot,
                production_shot,
                executions,
                candidates,
                default_candidate,
                source_status,
            )
            segment = timeline_by_shot[shot.id]
            projected_shots.append(
                WorkspaceShot(
                    shot_id=shot.id,
                    production_shot_id=production_shot.id if production_shot else None,
                    number=shot.order,
                    order=shot.order,
                    duration_seconds=float(shot.duration_seconds),
                    timeline_start_seconds=segment.start_seconds,
                    timeline_end_seconds=segment.end_seconds,
                    scene_id=shot.scene_id,
                    scene_label=(
                        f"{scene.title} · {location_labels.get(scene.location_id, scene.location_id)}"
                        if scene
                        else shot.scene_id
                    ),
                    character_ids=tuple(shot.subject),
                    character_labels=tuple(
                        character_labels.get(item, item) for item in shot.subject
                    ),
                    beat_ids=tuple(shot.source_script_beat_ids),
                    production_status=(
                        _enum_value(production_shot.status)
                        if production_shot
                        else "READY"
                    ),
                    qc_status=qc_status,
                    review_status=review_status,
                    final_source_status=source_status,
                    workspace_state=workspace_state,
                    preview_candidate_id=(
                        default_candidate.candidate_id if default_candidate else None
                    ),
                    references=references,
                    candidates=candidates,
                    continuity=self._continuity(
                        project_id,
                        current.current_job_id,
                        production_shot.id if production_shot else None,
                        shot.id,
                    ),
                )
            )
        beats = self._beats(script, projected_shots)
        states = {shot.workspace_state for shot in projected_shots}
        if projected_shots and states == {"ACCEPTED"}:
            workspace_state = "FINISHED"
        elif "BLOCKED" in states:
            workspace_state = "BLOCKED"
        elif projected_shots:
            workspace_state = "ACTIVE"
        else:
            workspace_state = "EMPTY"
        return DirectorWorkspaceProjection(
            project_id=project_id,
            script_revision_id=script_revision["id"],
            shot_plan_revision_id=plan_revision["id"],
            production_job_id=current.current_job_id,
            state=workspace_state,
            shots=tuple(projected_shots),
            beats=beats,
            timeline=timeline,
            gaps=gaps,
            total_duration_seconds=total,
            target_duration_seconds=float(project.target_duration_seconds),
            continuity_available=self.continuity_adapter is not None,
        )

    def _plan_revision(self, project_id: str, job_id: str | None):
        if job_id:
            job = self.repository.get_production_job(job_id)
            if job is not None:
                revision = self.repository.get_shot_revision(job.shot_plan_revision_id)
                if revision is not None and revision["project_id"] == project_id:
                    return revision
        return next(
            (
                item
                for item in self.repository.list_shot_revisions(project_id)
                if _enum_value(item["status"]) == "APPROVED"
            ),
            None,
        )

    @staticmethod
    def _shot_binding_ids(
        shot: object, scene: object | None
    ) -> tuple[tuple[str, str], ...]:
        bindings = [("CHARACTER", item) for item in shot.subject]
        if scene is not None:
            bindings.append(("LOCATION", scene.location_id))
        bindings.append(("SHOT", shot.id))
        return tuple(bindings)

    def _candidate_projection(
        self,
        project_id: str,
        shot: object,
        production_shot: object | None,
        executions: Sequence[object],
        qualified: object | None,
        exact_binding_ids: tuple[tuple[str, str], ...],
        character_labels: Mapping[str, str],
        location_labels: Mapping[str, str],
    ) -> tuple[WorkspaceCandidate, ...]:
        if production_shot is None:
            return ()
        output: list[WorkspaceCandidate] = []
        for execution in executions:
            artifacts = self.repository.list_production_artifacts(execution.id)
            if not self.final_sources._execution_contains_shot(
                execution, shot.id, production_shot.id, artifacts
            ):
                continue
            for artifact in artifacts:
                if not self.final_sources._is_supported_video_artifact(artifact):
                    continue
                qc_results = [
                    item
                    for item in self.repository.list_production_qc_results(
                        project_id, execution.id
                    )
                    if item.artifact_id == artifact.id
                ]
                qc = qc_results[-1] if qc_results else None
                metrics = (
                    tuple(
                        WorkspaceQCMetric(
                            name=item.metric_name,
                            status=_enum_value(item.status),
                            message=item.message,
                        )
                        for item in self.repository.list_production_qc_metrics(qc.id)
                    )
                    if qc is not None
                    else ()
                )
                reviews = (
                    self.repository.list_production_reviews(project_id, qc.id)
                    if qc is not None
                    else []
                )
                review = reviews[-1] if reviews else None
                analyses = [
                    item
                    for item in self.repository.list_vision_analyses(
                        project_id, execution.id
                    )
                    if item.artifact_id in {None, artifact.id}
                ]
                vision = analyses[-1] if analyses else None
                is_selected = bool(
                    qualified is not None
                    and qualified.production_execution_id == execution.id
                    and qualified.production_artifact_id == artifact.id
                )
                preview_path = self._artifact_path(project_id, artifact)
                references = self._snapshot_references(
                    project_id,
                    execution,
                    exact_binding_ids,
                    character_labels,
                    location_labels,
                )
                metadata = artifact.metadata_json or {}
                output.append(
                    WorkspaceCandidate(
                        candidate_id=f"{execution.id}:{artifact.id}",
                        execution_id=execution.id,
                        artifact_id=artifact.id,
                        label=f"Candidate {len(output) + 1:02d}",
                        execution_status=_enum_value(execution.status),
                        technical_qc_status=(
                            _enum_value(qc.status) if qc else "NOT_STARTED"
                        ),
                        technical_qc_metrics=metrics,
                        review_status=(
                            _enum_value(review.decision) if review else "PENDING"
                        ),
                        vision_status=(
                            _enum_value(vision.status) if vision else "NOT_RUN"
                        ),
                        vision_metrics=(dict(vision.metrics) if vision else {}),
                        is_selected_source=is_selected,
                        source_decision_id=(
                            qualified.source_decision_id if is_selected else None
                        ),
                        artifact_role=_enum_value(
                            metadata.get("artifact_role"), "PRODUCTION_ARTIFACT"
                        ),
                        preview_path=preview_path,
                        mime_type=str(
                            metadata.get("mime_type")
                            or metadata.get("media_type")
                            or "video/mp4"
                        ),
                        references=references,
                        created_at=str(
                            artifact.created_at or execution.created_at or ""
                        ),
                    )
                )
        return tuple(output)

    def _artifact_path(self, project_id: str, artifact: object) -> Path | None:
        try:
            path = self.final_sources._validate_source_path(project_id, artifact.path)
        except (FinalAssemblyServiceError, ValueError, OSError):
            return None
        return path if path.is_file() and path.stat().st_size > 0 else None

    def _snapshot_references(
        self,
        project_id: str,
        execution: object,
        exact_binding_ids: tuple[tuple[str, str], ...],
        character_labels: Mapping[str, str],
        location_labels: Mapping[str, str],
    ) -> tuple[WorkspaceReference, ...]:
        snapshot = execution.input_snapshot
        if snapshot is None:
            return ()
        exact = {f"{kind}:{binding_id}" for kind, binding_id in exact_binding_ids}
        output = []
        for binding_key, version_id in snapshot.reference_asset_versions.items():
            if binding_key not in exact:
                continue
            projected = self._version_reference(
                project_id,
                str(binding_key),
                str(version_id),
                character_labels,
                location_labels,
                provenance="EXECUTION_SNAPSHOT",
            )
            if projected is not None:
                output.append(projected)
        return tuple(output)

    def _current_references(
        self,
        project_id: str,
        exact_binding_ids: tuple[tuple[str, str], ...],
        character_labels: Mapping[str, str],
        location_labels: Mapping[str, str],
        story_revision_id: str | None,
    ) -> tuple[WorkspaceReference, ...]:
        output: list[WorkspaceReference] = []
        bindings = self.references.list_bindings(project_id)
        for kind, binding_id in exact_binding_ids:
            binding_type = ReferenceBindingType(kind)
            matches = [
                item
                for item in bindings
                if item.binding_type is binding_type and item.binding_id == binding_id
            ]
            selected = None
            for binding in reversed(matches):
                version = self.repository.get_reference_asset_version(
                    binding.asset_version_id
                )
                asset = (
                    self.repository.get_reference_asset(version.asset_id)
                    if version is not None
                    else None
                )
                if version is not None and asset is not None:
                    if asset.current_version_id == version.id:
                        selected = version
                        break
                    selected = selected or version
            if selected is None:
                continue
            projected = self._version_reference(
                project_id,
                f"{kind}:{binding_id}",
                selected.id,
                character_labels,
                location_labels,
                provenance="CURRENT_BINDING",
                story_revision_id=story_revision_id,
            )
            if projected is not None:
                output.append(projected)
        return tuple(output)

    def _version_reference(
        self,
        project_id: str,
        binding_key: str,
        version_id: str,
        character_labels: Mapping[str, str],
        location_labels: Mapping[str, str],
        *,
        provenance: str,
        story_revision_id: str | None = None,
    ) -> WorkspaceReference | None:
        version = self.repository.get_reference_asset_version(version_id)
        if version is None or version.project_id != project_id:
            return None
        asset = self.repository.get_reference_asset(version.asset_id)
        if asset is None or asset.project_id != project_id:
            return None
        kind, _, binding_id = binding_key.partition(":")
        kind = kind.upper()
        labels = character_labels if kind == "CHARACTER" else location_labels
        label = labels.get(binding_id, binding_id)
        locked = asset.current_version_id == version.id
        if kind in {"CHARACTER", "LOCATION"}:
            try:
                locked = self.references.is_binding_ready(
                    project_id,
                    ReferenceBindingType(kind),
                    binding_id,
                    story_revision_id,
                )
            except (ReferenceAssetServiceError, ValueError):
                locked = False
        thumbnail = None
        if version.mime_type.casefold().startswith("image/"):
            try:
                path = self.references.resolve_version_path(project_id, version.id)
                if path.is_file():
                    thumbnail = path
            except (ReferenceAssetServiceError, OSError):
                pass
        return WorkspaceReference(
            binding_kind=kind,
            binding_id=binding_id,
            label=label,
            version_id=version.id,
            version_number=version.version_number,
            locked=locked,
            provenance=provenance,
            thumbnail_path=thumbnail,
        )

    def _continuity(
        self,
        project_id: str,
        production_job_id: str | None,
        production_shot_id: str | None,
        shot_id: str,
    ) -> WorkspaceContinuity:
        if self.continuity_adapter is None:
            return WorkspaceContinuity(False, "NOT_AVAILABLE")
        try:
            value = self.continuity_adapter(
                project_id=project_id,
                production_job_id=production_job_id,
                production_shot_id=production_shot_id,
                shot_id=shot_id,
            )
        except Exception:
            return WorkspaceContinuity(True, "ERROR", ("连续性投影暂不可读",))
        if not isinstance(value, Mapping):
            return WorkspaceContinuity(True, "NOT_RUN")
        raw_warnings = value.get("warnings", ())
        warnings = (
            tuple(str(item)[:240] for item in raw_warnings)
            if isinstance(raw_warnings, (list, tuple))
            else ()
        )
        status = _enum_value(
            value.get("status"), "CLEAR" if not warnings else "WARNING"
        )
        return WorkspaceContinuity(True, status, warnings)

    @staticmethod
    def _source_status(candidate: WorkspaceCandidate | None) -> str:
        if candidate is None or not candidate.is_selected_source:
            return "NONE"
        if candidate.source_decision_id:
            return "SELECTED"
        if candidate.review_status in {"APPROVED", "ACCEPTED"}:
            return "ACCEPTED"
        return "QUALIFIED"

    def _workspace_state(
        self,
        shot: object,
        production_shot: object | None,
        executions: Sequence[object],
        candidates: Sequence[WorkspaceCandidate],
        candidate: WorkspaceCandidate | None,
        source_status: str,
    ) -> str:
        if source_status in {"SELECTED", "ACCEPTED"}:
            return "ACCEPTED"
        matching_executions = []
        if production_shot is not None:
            for execution in executions:
                artifacts = self.repository.list_production_artifacts(execution.id)
                if self.final_sources._execution_contains_shot(
                    execution, shot.id, production_shot.id, artifacts
                ):
                    matching_executions.append(execution)
        if any(
            item.status
            in {ProductionExecutionStatus.QUEUED, ProductionExecutionStatus.RUNNING}
            for item in matching_executions
        ):
            return "GENERATING"
        if (
            production_shot is not None
            and production_shot.status is ProductionShotStatus.FAILED
        ):
            return "BLOCKED"
        if candidate is None:
            return "READY"
        if candidate.review_status == ProductionReviewDecision.REJECTED.value:
            return "BLOCKED"
        if candidate.technical_qc_status == ProductionQCStatus.QC_FAILED.value:
            return "BLOCKED"
        if candidate.technical_qc_status in {
            ProductionQCStatus.QC_PENDING.value,
            ProductionQCStatus.QC_RUNNING.value,
            "NOT_STARTED",
        }:
            return "QC"
        if candidate.technical_qc_status == ProductionQCStatus.QC_PASS.value:
            return "WAITING_HUMAN"
        return "READY"

    @staticmethod
    def _beats(
        script: object, shots: Sequence[WorkspaceShot]
    ) -> tuple[WorkspaceBeat, ...]:
        output: list[WorkspaceBeat] = []
        for scene in sorted(script.scenes, key=lambda item: (item.order, item.id)):
            scene_shots = [item for item in shots if item.scene_id == scene.id]
            for beat in sorted(scene.beats, key=lambda item: (item.order, item.id)):
                exact = [
                    item.shot_id for item in scene_shots if beat.id in item.beat_ids
                ]
                mapped = exact or [item.shot_id for item in scene_shots]
                output.append(
                    WorkspaceBeat(
                        beat_id=beat.id,
                        beat_order=beat.order,
                        scene_id=scene.id,
                        scene_title=scene.title,
                        text=beat.text,
                        beat_type=_enum_value(beat.type),
                        shot_ids=tuple(mapped),
                        mapping_kind="EXACT" if exact else "SCENE_FALLBACK",
                    )
                )
        return tuple(output)


__all__ = [
    "DirectorWorkspaceProjection",
    "DirectorWorkspaceProjectionService",
    "TimelineGap",
    "TimelineSegment",
    "WorkspaceBeat",
    "WorkspaceCandidate",
    "WorkspaceContinuity",
    "WorkspaceQCMetric",
    "WorkspaceReference",
    "WorkspaceShot",
    "build_timeline",
]
