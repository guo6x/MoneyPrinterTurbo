"""Viewer-first QC and human review workspace.

The page deliberately keeps three decisions independent: deterministic
technical QC, optional Vision QC, and the human review decision.  Runtime and
provider objects stay behind service facades.  The technical action is wired
directly to ``ProductionQCService.run_qc``; ``重新生成`` is reserved for the
guarded creative-regeneration facade and can never be a QC retry alias.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.domain import (
    ProductionExecutionStatus,
    ProductionQCStatus,
    ProductionReviewDecision,
)
from aidrama_studio.pages._shared import (
    current_project_or_stop,
    render_background_activity,
    render_project_context,
)
from aidrama_studio.services import (
    FinalAssemblyService,
    FinalAssemblyServiceError,
    ProductionExecutionService,
    ProductionExecutionServiceError,
    ProductionQCService,
    ProductionQCServiceError,
    ProductionService,
    VisionQCService,
)
from aidrama_studio.services.vision_qc import VisionQCError


def _safe_ui_error(_exc: object, fallback: str) -> str:
    """Keep provider, path and identifier details inside Advanced diagnostics."""

    return fallback


def _value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def _status(item: Any, key: str = "status", default: str = "UNKNOWN") -> str:
    value = _value(item, key, default)
    return str(getattr(value, "value", value)).strip().upper()


def _safe_path(value: object) -> str:
    """Return a project-relative-looking label without leaking local paths."""

    if not isinstance(value, str) or not value.strip():
        return "—"
    normalized = value.strip().replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        normalized.startswith("/")
        or PureWindowsPath(value).drive
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return "项目相对文件"
    return PurePosixPath(*parts).as_posix()


def _human_qc_state(status: str) -> str:
    return {
        ProductionQCStatus.QC_PASS.value: "技术检查通过",
        ProductionQCStatus.QC_FAILED.value: "技术检查未通过",
        ProductionQCStatus.QC_RUNNING.value: "技术检查中",
        ProductionQCStatus.QC_PENDING.value: "等待技术检查",
    }.get(status, "等待技术检查")


def _human_review_state(decision: str) -> str:
    return {
        ProductionReviewDecision.APPROVED.value: "已通过",
        ProductionReviewDecision.REJECTED.value: "需要修改",
        ProductionReviewDecision.PENDING.value: "等待人工决定",
    }.get(decision, "等待人工决定")


def _latest_review(reviews: Sequence[Any]) -> Any | None:
    values = list(reviews)
    return values[-1] if values else None


def _latest_rejected_review(reviews: Sequence[Any]) -> Any | None:
    # Regeneration is valid only for the *current* review decision.  An older
    # rejection must not remain actionable after a later approval or revision.
    latest = _latest_review(reviews)
    if latest is not None and _status(latest, "decision", "PENDING") == ProductionReviewDecision.REJECTED.value:
        return latest
    return None


def _result_shot_id(result: Any, execution: Any) -> str:
    """Recover the canonical Shot identity from persisted review provenance."""

    direct = str(_value(result, "shot_id", "") or "").strip()
    if direct:
        return direct
    snapshot = _value(execution, "input_snapshot")
    parameters = _value(snapshot, "shot_parameters", {}) if snapshot is not None else {}
    if isinstance(parameters, Mapping) and len(parameters) == 1:
        return str(next(iter(parameters))).strip()
    return ""


def _run_qc(
    service: ProductionQCService,
    project: Any,
    execution: Any,
    artifact_id: str | None,
) -> Any | None:
    """Run deterministic QC directly (never through a retry alias)."""

    execution_id = _value(execution, "id")
    if (
        not execution_id
        or artifact_id is None
        or _status(execution) != ProductionExecutionStatus.SUCCEEDED.value
    ):
        st.warning("当前镜头尚未完成制作，暂时不能进行技术检查。")
        return None
    try:
        result = service.run_qc(project.id, execution_id, artifact_id)
    except (ProductionQCServiceError, ValueError, TypeError) as exc:
        st.error(_safe_ui_error(exc, "技术检查未完成，请稍后重试。"))
        return None
    st.success("技术检查已运行；不会创建新的生成任务。")
    return result


def _submit_human_review(
    service: ProductionQCService,
    project: Any,
    result: Any,
    decision: str,
    notes: str,
) -> Any | None:
    try:
        created = service.create_review(
            project.id,
            _value(result, "id"),
            ProductionReviewDecision(decision),
            reviewer="AIDrama user",
            notes=notes,
        )
    except (ProductionQCServiceError, ValueError, TypeError) as exc:
        st.error(_safe_ui_error(exc, "审片决定未保存，请稍后重试。"))
        return None
    st.success("人工审片决定已保存；历史技术检查保持不变。")
    return created


def _request_creative_regeneration(
    execution_service: ProductionExecutionService,
    project: Any,
    job: Any,
    shot: Any,
    result: Any,
    reviews: Sequence[Any],
) -> tuple[Any, Any] | None:
    """Append a guarded creative attempt after an explicit rejection.

    The helper requires the latest rejected review and immutable input
    snapshot. It does not call a provider or perform a paid submission itself.
    """

    rejected = _latest_rejected_review(reviews)
    execution = _value(result, "execution")
    if execution is None:
        get_execution = getattr(execution_service, "get_execution", None)
        if callable(get_execution):
            try:
                execution = get_execution(project.id, _value(result, "execution_id"))
            except Exception:
                execution = None
    snapshot = _value(execution, "input_snapshot") if execution is not None else None
    request_method = getattr(execution_service, "request_creative_regeneration", None)
    result_id = _value(result, "id")
    rejected_result_id = _value(rejected, "qc_result_id") if rejected is not None else None
    if (
        rejected is None
        or (rejected_result_id is not None and str(rejected_result_id) != str(result_id))
        or _status(result) != ProductionQCStatus.QC_PASS.value
        or _status(execution) != ProductionExecutionStatus.SUCCEEDED.value
        or snapshot is None
        or not callable(request_method)
        or job is None
        or shot is None
    ):
        st.warning("请先保存“需要修改”的审片决定；只有带有冻结创作快照的镜头才能重新生成。")
        return None
    try:
        created = request_method(
            project.id,
            _value(job, "id"),
            _value(shot, "id", _value(shot, "shot_id")),
            _value(rejected, "id"),
            snapshot,
            worker_type=str(_value(execution, "worker_type", "mpt")),
            runtime_plan_id=_value(execution, "runtime_plan_id"),
            generation_brief_id=_value(execution, "generation_brief_id"),
        )
    except (ProductionExecutionServiceError, ValueError, TypeError) as exc:
        st.error(_safe_ui_error(exc, "重新生成未加入队列，请检查生成意图后重试。"))
        return None
    except Exception as exc:
        st.error(_safe_ui_error(exc, "重新生成未加入队列，请稍后重试。"))
        return None
    st.success("新的创作版本已加入后台队列；原视频与审片记录仍保留。")
    return created


def _render_vision_summary(
    vision_service: VisionQCService | Any | None,
    project: Any,
    execution: Any,
    artifact: Any,
) -> None:
    """Render Vision QC as a clearly advisory, independent check."""

    st.markdown("#### Vision QC（辅助建议）")
    st.caption("Vision QC 只提供人物、场景和动作建议，不会替代技术检查或人工决定。")
    if vision_service is None or artifact is None:
        st.info("Vision QC 尚未运行。")
        return
    key = f"review-vision-result-{_value(execution, 'id', 'execution')}"
    result = st.session_state.get(key)
    if result is None:
        latest = getattr(vision_service, "latest", None)
        if callable(latest):
            try:
                result = latest(project.id, _value(execution, "id"))
            except (VisionQCError, ValueError, TypeError):
                result = None
    if result is None:
        st.info("Vision QC 尚未运行。")
        if st.button(
            "运行 Vision QC",
            key=f"run-vision-{_value(execution, 'id', 'execution')}",
        ):
            try:
                result = vision_service.analyze(
                    project.id,
                    _value(execution, "id"),
                    _value(artifact, "id"),
                )
            except (VisionQCError, ValueError, TypeError) as exc:
                st.warning(_safe_ui_error(exc, "画面分析暂不可用；不影响技术检查和人工审片。"))
            else:
                st.session_state[key] = result
                st.rerun()
        return
    vision_status = str(_value(result, "status", "NOT_RUN")).upper()
    if vision_status == "NOT_RUN":
        st.info("Vision QC 未运行（当前能力或授权不足）。")
    elif vision_status in {"PASS", "AI_ANALYSIS", "SUCCEEDED"}:
        st.success("Vision QC 已完成；请结合画面自行判断。")
    else:
        st.warning("Vision QC 返回了需要关注的结果；不影响技术检查状态。")
    metrics = _value(result, "metrics", {}) or {}
    if isinstance(metrics, Mapping):
        for name, metric in metrics.items():
            score = metric.get("score") if isinstance(metric, Mapping) else None
            severity = (
                metric.get("severity", metric.get("status", "建议"))
                if isinstance(metric, Mapping)
                else "建议"
            )
            reason = (
                metric.get("reason", metric.get("summary", ""))
                if isinstance(metric, Mapping)
                else ""
            )
            detail = f" · {reason}" if reason else ""
            st.caption(
                f"{str(name).replace('_', ' ')} · {severity} · "
                f"{score if score is not None else '有建议'}{detail}"
            )
    with st.expander("Vision 诊断详情", expanded=False):
        st.caption("分析来源、帧清单和模型标识仅供排障；不会影响人工审片决定。")
        public = getattr(result, "__dict__", result)
        if isinstance(public, Mapping):
            safe = {
                key: value
                for key, value in public.items()
                if key not in {"project_id", "execution_id", "artifact_id", "analysis_id", "frame_manifest_id"}
            }
            st.json(safe)


def _render_candidate_comparison(
    execution_service: ProductionExecutionService,
    qc_service: ProductionQCService,
    project: Any,
    execution: Any,
    *,
    source_service: FinalAssemblyService | Any | None = None,
    job: Any | None = None,
    shot: Any | None = None,
) -> list[dict[str, Any]]:
    """Show a safe candidate comparison while hiding technical identifiers."""

    try:
        artifacts = list(execution_service.list_artifacts(project.id, _value(execution, "id")))
    except Exception:
        artifacts = []
    try:
        results = list(qc_service.list_results(project.id, _value(execution, "id")))
    except Exception:
        results = []
    candidates: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts, start=1):
        artifact_id = _value(artifact, "id")
        matching = [item for item in results if _value(item, "artifact_id") in {None, artifact_id}]
        qc_result = matching[-1] if matching else None
        try:
            reviews = (
                qc_service.list_reviews(project.id, _value(qc_result, "id"))
                if qc_result is not None
                else []
            )
        except Exception:
            reviews = []
        candidates.append({"artifact": artifact, "result": qc_result, "reviews": reviews})
        status = _human_qc_state(_status(qc_result, default="QC_PENDING"))
        review = _human_review_state(_status(_latest_review(reviews), "decision", "PENDING"))
        with st.container(border=True):
            st.markdown(f"**候选 {index}** · {status} · {review}")
            metadata = _value(artifact, "metadata_json", {}) or {}
            if isinstance(metadata, Mapping):
                details = []
                duration = metadata.get("duration_seconds") or metadata.get("duration")
                resolution = metadata.get("resolution")
                if duration is not None:
                    try:
                        details.append(f"{float(duration):g} 秒")
                    except (TypeError, ValueError):
                        pass
                if resolution:
                    details.append(str(resolution))
                if details:
                    st.caption(" · ".join(details))
            if qc_result is not None and _status(qc_result) == ProductionQCStatus.QC_PASS.value:
                st.caption("候选已通过技术检查，可在人工审片后进入成片。")
                latest_review = _latest_review(reviews)
                can_lock = (
                    source_service is not None
                    and job is not None
                    and shot is not None
                    and latest_review is not None
                    and _status(latest_review, "decision") == ProductionReviewDecision.APPROVED.value
                    and callable(getattr(source_service, "select_shot_source", None))
                )
                if can_lock and st.button(
                    "锁定此候选",
                    key=f"lock-candidate-{project.id}-{_value(artifact, 'id', index)}",
                ):
                    try:
                        source_service.select_shot_source(
                            project.id,
                            _value(job, "id"),
                            _value(shot, "id", _value(shot, "shot_id")),
                            production_execution_id=_value(execution, "id"),
                            production_artifact_id=artifact_id,
                            selected_by="AIDrama user",
                        )
                    except (FinalAssemblyServiceError, ValueError, TypeError) as exc:
                        st.warning(_safe_ui_error(exc, "候选暂时无法锁定，请确认人工审片状态。"))
                    except Exception as exc:
                        st.warning(_safe_ui_error(exc, "候选暂时无法锁定，请稍后重试。"))
                    else:
                        st.success("候选已锁定；成片准备度会重新计算。")
                        st.rerun()
    return candidates


def _render_result(
    service: ProductionQCService,
    project: Any,
    result: Any,
    *,
    execution_service: ProductionExecutionService | Any | None = None,
    job: Any | None = None,
    shot: Any | None = None,
    vision_service: VisionQCService | Any | None = None,
) -> None:
    """Render one viewer-first QC result with human labels."""

    result_id = str(_value(result, "id", ""))
    status = _status(result)
    summary = _value(result, "summary_json", {}) or {}
    if not isinstance(summary, Mapping):
        summary = {}
    execution = _value(result, "execution")
    if execution is None and execution_service is not None:
        get_execution = getattr(execution_service, "get_execution", None)
        if callable(get_execution):
            try:
                execution = get_execution(project.id, _value(result, "execution_id"))
            except Exception:
                execution = None
    artifact = None
    if execution_service is not None and execution is not None:
        try:
            artifacts = execution_service.list_artifacts(project.id, _value(execution, "id"))
            artifact_id = _value(result, "artifact_id")
            artifact = next((item for item in artifacts if _value(item, "id") == artifact_id), artifacts[0] if artifacts else None)
        except Exception:
            artifact = None

    with st.container(border=True):
        shot_label = _value(result, "shot_number", None)
        if shot_label is None:
            shot_label = "当前镜头"
        st.markdown(f"### 镜头 {shot_label} · 看片")
        st.caption("先看画面，再分别查看技术检查、Vision 建议和人工审片状态。")
        media_path = _value(result, "media_path", _value(result, "artifact_path"))
        if media_path is None and artifact is not None:
            media_path = _value(artifact, "media_path", _value(artifact, "path"))
        if media_path:
            try:
                playback_path = (
                    service.resolve_artifact_path(project.id, str(media_path))
                    if artifact is not None
                    else media_path
                )
                st.video(str(playback_path))
            except (ProductionQCServiceError, OSError, TypeError, ValueError):
                st.caption("预览暂不可用")
        else:
            st.info("暂无可预览媒体；可以先查看检查结果。")

        left, right = st.columns(2)
        left.metric("技术检查", _human_qc_state(status))
        right.metric("检查摘要", f"通过 {summary.get('passed', 0)} · 需处理 {summary.get('failed', 0)}")

        try:
            reviews = list(service.list_reviews(project.id, result_id))
        except Exception:
            reviews = []
        latest = _latest_review(reviews)
        st.caption(f"人工审片 · {_human_review_state(_status(latest, 'decision', 'PENDING'))}")
        if latest is not None and _value(latest, "notes"):
            st.caption(f"备注 · {_value(latest, 'notes')}")

        # This exact label/action pair is the P0 semantic boundary.
        technical_col, creative_col = st.columns(2)
        with technical_col:
            if st.button(
                "重新运行技术检查",
                key=f"rerun-technical-qc-{result_id}",
                disabled=execution is None or _status(execution) != ProductionExecutionStatus.SUCCEEDED.value,
            ):
                _run_qc(service, project, execution, _value(result, "artifact_id"))
                st.rerun()
        with creative_col:
            can_regenerate = (
                execution_service is not None
                and job is not None
                and shot is not None
                and _latest_rejected_review(reviews) is not None
                and execution is not None
                and _status(execution) == ProductionExecutionStatus.SUCCEEDED.value
                and _value(execution, "input_snapshot") is not None
                and status == ProductionQCStatus.QC_PASS.value
            )
            if st.button("重新生成", key=f"creative-regenerate-{result_id}", disabled=not can_regenerate):
                wrapped = dict(result) if isinstance(result, Mapping) else {"id": result_id}
                wrapped["execution"] = execution
                _request_creative_regeneration(execution_service, project, job, shot, wrapped, reviews)
                st.rerun()

        with st.expander("技术检查明细（高级）", expanded=False):
            try:
                metrics = service.list_metrics(project.id, result_id)
            except Exception:
                metrics = []
            for metric in metrics:
                metric_status = _status(metric)
                icon = "✓" if metric_status == "PASS" else ("!" if metric_status == "FAIL" else "·")
                st.markdown(
                    f"{icon} **{str(_value(metric, 'metric_name', '检查项')).replace('_', ' ')}** · "
                    f"{metric_status} · {_value(metric, 'message', '')}"
                )
            report_path = _value(result, "report_path")
            if report_path:
                st.caption(f"技术报告 · {_safe_path(report_path)}")
            st.caption("内部执行标识、文件哈希和原始事件仅供排障，不参与人工决定。")

        if vision_service is not None:
            _render_vision_summary(vision_service, project, execution, artifact)

        if status == ProductionQCStatus.QC_PASS.value:
            with st.form(f"review-form-{result_id}"):
                decision = st.selectbox(
                    "人工决定",
                    [ProductionReviewDecision.APPROVED.value, ProductionReviewDecision.REJECTED.value],
                    format_func=lambda value: "通过" if value == "APPROVED" else "修改生成意图后重做",
                    key=f"review-decision-{result_id}",
                )
                notes = st.text_area("审片备注", key=f"review-notes-{result_id}", height=70)
                if st.form_submit_button("保存人工决定"):
                    _submit_human_review(service, project, result, decision, notes)
                    st.rerun()


def _canonical_job(
    production_service: ProductionService | Any,
    project: Any,
    state_service: Any | None = None,
) -> tuple[Any | None, Any | None, list[Any]]:
    """Select canonical current job, with a fixture-safe compatibility fallback."""

    if state_service is None:
        try:
            from aidrama_studio.services.current_state import CurrentProductionStateService

            state_service = CurrentProductionStateService(getattr(production_service, "repository", None))
        except Exception:
            state_service = None
    if state_service is not None:
        try:
            state = state_service.derive(project.id)
            jobs = list(production_service.list_jobs(project.id) or [])
            if state.job is not None:
                return state.job, state, jobs
        except Exception:
            pass
    try:
        jobs = list(production_service.list_jobs(project.id) or [])
    except Exception:
        return None, None, []
    return (jobs[-1] if jobs else None), None, jobs


def _activity_for_review(project: Any, execution_service: Any, job: Any | None) -> tuple[dict[str, object], ...]:
    if job is None:
        return ()
    try:
        executions = list(execution_service.list_executions(project.id, _value(job, "id")))
    except Exception:
        return ()
    active = [item for item in executions if _status(item) in {"QUEUED", "RUNNING"}]
    if not active:
        return ()
    execution = active[-1]
    return (
        {
            "activity_id": _value(execution, "id"),
            "title": "镜头制作仍在后台进行",
            "detail": "可以继续查看已完成镜头；完成后返回审片。",
            "state": _status(execution).lower(),
            "next_action": "查看制作",
            "next_page": "production",
        },
    )


def render() -> None:
    page_header("审片", "REVIEW WORKSPACE", "以画面为中心完成技术检查、Vision 建议与人工决定。")
    project = current_project_or_stop()
    render_project_context(project, stage="审片", next_action="完成审片", next_page="review")

    qc_service = ProductionQCService()
    repository = getattr(qc_service, "repository", None)
    production_service = ProductionService(repository)
    execution_service = ProductionExecutionService(repository)
    try:
        vision_service: Any | None = VisionQCService(repository)
    except Exception:
        vision_service = None

    job, canonical_state, jobs = _canonical_job(production_service, project)
    render_background_activity(_activity_for_review(project, execution_service, job), compact=True)
    if job is None:
        st.info("还没有可审片的制作结果。请先完成镜头制作。")
        if st.button("去制作", key=f"review-production-{project.id}"):
            from aidrama_studio.components.navigation import request_navigation

            request_navigation("production")
        return

    try:
        executions = list(execution_service.list_executions(project.id, _value(job, "id")))
    except Exception as exc:
        st.warning(_safe_ui_error(exc, "审片结果暂不可用，请稍后刷新。"))
        return
    if not executions:
        st.info("该项目还没有可查看的镜头结果。")
        return
    selected_execution = executions[-1]
    try:
        artifacts = list(execution_service.list_artifacts(project.id, _value(selected_execution, "id")))
    except Exception:
        artifacts = []
    try:
        results = list(qc_service.list_results(project.id, _value(selected_execution, "id")))
    except Exception as exc:
        st.warning(_safe_ui_error(exc, "技术检查结果暂不可用，请稍后刷新。"))
        results = []

    selected_shot = None
    if results:
        selected_result_key = f"review-result-{project.id}"
        selected_result_id = st.session_state.get(selected_result_key)
        selected_result = next(
            (item for item in results if str(_value(item, "id")) == str(selected_result_id)),
            results[-1],
        )
        if len(results) > 1:
            selected_index = st.selectbox(
                "镜头结果",
                list(range(len(results))),
                index=results.index(selected_result),
                format_func=lambda index: f"镜头 {index + 1} · {_human_qc_state(_status(results[index]))}",
                key=f"review-result-select-{project.id}",
            )
            selected_result = results[int(selected_index)]
            st.session_state[selected_result_key] = _value(selected_result, "id")
        shot = None
        if canonical_state is not None:
            result_shot_id = _result_shot_id(selected_result, selected_execution)
            if result_shot_id:
                shot = next(
                    (
                        item
                        for item in getattr(canonical_state, "shots", ())
                        if str(_value(item, "id")) == result_shot_id
                        or str(_value(item, "shot_id")) == result_shot_id
                    ),
                    None,
                )
        selected_shot = shot
        _render_result(
            qc_service,
            project,
            selected_result,
            execution_service=execution_service,
            job=job,
            shot=shot,
            vision_service=vision_service,
        )
    else:
        st.info("尚无技术检查结果。先运行一次技术检查，再进行人工审片。")
        if artifacts:
            if st.button("运行技术检查", type="primary", key=f"run-qc-{_value(selected_execution, 'id')}"):
                _run_qc(qc_service, project, selected_execution, _value(artifacts[0], "id"))
                st.rerun()
        else:
            st.warning("该镜头还没有可检查的媒体。")

    source_service = None
    if repository is not None:
        try:
            source_service = FinalAssemblyService(repository)
        except Exception:
            source_service = None
    with st.expander("候选对比 / 锁定", expanded=False):
        st.caption("选择候选不会自动通过镜头；通过仍需在人工审片中明确确认。")
        _render_candidate_comparison(
            execution_service,
            qc_service,
            project,
            selected_execution,
            source_service=source_service,
            job=job,
            shot=selected_shot,
        )

    with st.expander("审批记录", expanded=False):
        all_reviews: list[Any] = []
        for result in results:
            try:
                all_reviews.extend(qc_service.list_reviews(project.id, _value(result, "id")))
            except Exception:
                continue
        if not all_reviews:
            st.caption("暂无人工审片记录。")
        for review in reversed(all_reviews):
            st.caption(
                f"{_human_review_state(_status(review, 'decision'))} · "
                f"{_value(review, 'reviewer', '项目成员')} · {_value(review, 'notes', '')}"
            )

    with st.expander("执行详情（高级）", expanded=False):
        st.caption("仅供排障；不会改变 QC、候选或人工决定。")
        st.caption(f"job={_value(job, 'id', '—')} · execution={_value(selected_execution, 'id', '—')}")
        for artifact in artifacts:
            st.caption(f"artifact={_value(artifact, 'id', '—')} · path={_safe_path(_value(artifact, 'path'))}")
        if results:
            st.json(
                [
                    {
                        "id": _value(item, "id"),
                        "status": _status(item),
                        "report_path": _safe_path(_value(item, "report_path")),
                    }
                    for item in results
                ]
            )


__all__ = [
    "render",
    "_render_result",
    "_request_creative_regeneration",
    "_result_shot_id",
    "_run_qc",
    "_safe_path",
]
