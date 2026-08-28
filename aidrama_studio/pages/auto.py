"""AI 制作台 (AUTO) page.

This page is deliberately a projection layer.  The orchestrator, current
production-state service, and persisted event log remain the only workflow
truths; Streamlit renders those facts and routes the creator to the formal
service-owned gates.  No provider task is created implicitly while rendering.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
import re
from typing import Any

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.components.workflow_progress import (
    PIPELINE_DEFINITIONS,
    PIPELINE_STAGE_LABELS,
    PipelineStageProjection,
    pipeline_stage_projections,
    render_pipeline,
)
from aidrama_studio.pages._shared import (
    CAPABILITY_DEFINITIONS,
    current_project_or_stop,
    normalize_capability_snapshots,
    render_project_context,
)
from aidrama_studio.domain import AutoAction, AutoRunStatus
from aidrama_studio.services import AutoOrchestratorError, AutoOrchestratorService


# Kept as a compatibility alias for callers that imported the old page-level
# stage map.  The canonical rail map lives in workflow_progress.py.
_STAGE_LABELS = {
    **PIPELINE_STAGE_LABELS,
    "COMPLETED": "成片",
}

_STATUS_LABELS: dict[str, str] = {
    AutoRunStatus.IDLE.value: "待开始",
    AutoRunStatus.RUNNING.value: "正在推进",
    AutoRunStatus.WAITING_PROVIDER.value: "AI 生成中",
    AutoRunStatus.WAITING_HUMAN.value: "等待你的确认",
    AutoRunStatus.BLOCKED.value: "需要处理",
    AutoRunStatus.FAILED.value: "未完成",
    AutoRunStatus.SUCCEEDED.value: "制作完成",
    AutoRunStatus.CANCELLED.value: "已取消",
}

_ACTION_LABELS: dict[str, str] = {
    "GENERATE_OR_CREATE_STORY": "生成故事",
    "GENERATE_SCRIPT": "生成剧本",
    "GENERATE_SHOT_PLAN": "生成分镜",
    "GENERATE_REFERENCE_CANDIDATE": "处理参考资产",
    "PREPARE_PRODUCTION": "准备制作",
    "CREATE_PRODUCTION_EXECUTION": "开始视频制作",
    "POLL_EXISTING_TASK": "检查最新进度",
    "RUN_TECHNICAL_QC": "检查技术质量",
    "RUN_OPTIONAL_VISION_QC": "检查画面质量",
    "WAITING_HUMAN": "等待你的确认",
    "PAID_AUTHORIZATION_REQUIRED": "授权并继续",
    "FINAL_ASSEMBLY": "生成成片",
    "NONE": "无需操作",
}
# Only actions with an intentional creator-facing translation are executable
# from this page.  If the service grows a new enum before the UI is updated,
# it must take the route-only unknown fallback below rather than calling
# ``resume`` with an unreviewed action.
_KNOWN_ACTIONS = frozenset(_ACTION_LABELS)
_KNOWN_STATUSES = frozenset(_STATUS_LABELS)

_HUMAN_ROUTES: dict[str, tuple[str, str]] = {
    # Keep the original two-field route contract for adjacent callers while
    # storing creator-facing copy for the explanatory sentence separately.
    "APPROVE_STORY": ("去确认故事", "story"),
    "APPROVE_SCRIPT": ("去确认剧本", "story"),
    "APPROVE_SHOT_PLAN": ("去确认分镜", "director"),
    "PROMOTE_BIND_AND_LOCK_REFERENCE": ("去确认参考资产", "assets"),
    "BIND_AND_LOCK_REFERENCE": ("去确认参考资产", "assets"),
    "APPROVE_OR_REJECT_PRODUCTION_REVIEW": ("前往审片", "review"),
    "INSPECT_FAILURE_AND_RESUME": ("检查失败原因", "settings"),
    "INSPECT_BLOCKER": ("处理阻塞项", "production"),
    "RESUME_AUTO_MODE": ("继续自动制作", "auto"),
}

_HUMAN_COMPLETED: dict[str, str] = {
    "APPROVE_STORY": "故事草稿",
    "APPROVE_SCRIPT": "剧本草稿",
    "APPROVE_SHOT_PLAN": "分镜方案",
    "PROMOTE_BIND_AND_LOCK_REFERENCE": "参考资产候选",
    "BIND_AND_LOCK_REFERENCE": "参考资产版本",
    "APPROVE_OR_REJECT_PRODUCTION_REVIEW": "视频制作结果",
    "INSPECT_FAILURE_AND_RESUME": "当前步骤",
    "INSPECT_BLOCKER": "当前生成任务",
    "RESUME_AUTO_MODE": "当前流程",
}

_STAGE_HUMAN_FALLBACKS: dict[str, tuple[str, str, str]] = {
    "STORY": ("去确认故事", "story", "故事草稿"),
    "SCRIPT": ("去确认剧本", "story", "剧本草稿"),
    "SHOT_PLAN": ("去确认分镜", "director", "分镜方案"),
    "REFERENCES": ("去确认参考资产", "assets", "参考资产"),
    "REVIEW": ("前往审片", "review", "视频制作结果"),
}

_BLOCKING_LABELS: dict[str, tuple[str, str, str, str]] = {
    "UNCERTAIN_CREATE": (
        "生成状态需要确认",
        "现有生成请求的状态还需要核对。为避免重复创建，我们不会再次提交生成请求。",
        "处理生成状态",
        "production",
    ),
    "RECONCILIATION_REQUIRED": (
        "生成状态需要确认",
        "任务结果尚未完成对账。为避免重复创建，我们不会再次提交生成请求。",
        "处理生成状态",
        "production",
    ),
    "SUBMISSION_UNCERTAIN": (
        "生成状态需要确认",
        "提交结果需要确认。请进入正式处理路径，我们不会再次提交生成请求。",
        "处理生成状态",
        "production",
    ),
    "MAX_STEPS_REACHED": (
        "流程暂时停下",
        "本轮自动步骤已到达安全上限；你可以确认当前项目后继续。",
        "继续自动制作",
        "auto",
    ),
}

_CAPABILITY_LABELS: dict[str, str] = {
    "LLM": "创作 AI",
    "IMAGE": "参考图",
    "VIDEO": "视频生成",
    "VISION": "画面分析",
    "TTS": "配音",
}
_CAPABILITY_STATE_LABELS = frozenset(
    {"已配置", "需要配置", "待验证", "运行不可用", "暂不可用", "配置有误", "需要确认"}
)


@dataclass(frozen=True, slots=True)
class DominantActionProjection:
    """The one creator-facing action presented in the first fold."""

    title: str
    reason: str
    cta: str
    mode: str
    route: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "title": self.title,
            "reason": self.reason,
            "cta": self.cta,
            "mode": self.mode,
            "route": self.route,
        }

    def __getitem__(self, key: str) -> str | None:
        return self.as_dict()[key]


def _enum_text(value: Any) -> str:
    candidate = getattr(value, "value", value)
    return str(candidate or "").strip().upper()


def _flag(value: Any) -> bool:
    """Read a durable boolean without treating arbitrary strings as truthy."""

    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "yes", "1", "on"}
    return False


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        try:
            return getattr(value, name)
        except AttributeError:
            continue
    return default


def _strict_int(value: Any, *, minimum: int) -> int:
    """Coerce a preview number without accepting booleans or fractions."""

    if isinstance(value, bool):
        raise ValueError("boolean is not a budget value")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        result = int(value.strip())
    else:
        raise ValueError("budget value is not an integer")
    if result < minimum:
        raise ValueError("budget value is below the safe minimum")
    return result


def status_label(value: Any) -> str:
    """Map an AUTO status to creator language without exposing raw enums."""

    return _STATUS_LABELS.get(_enum_text(value), "状态待确认")


def action_label(value: Any) -> str:
    """Map every current ``AutoAction`` to a safe creator-facing label."""

    return _ACTION_LABELS.get(_enum_text(value), "查看下一步")


# Backward/semantic aliases make the mapping easy to discover in tests and
# adjacent page code.
human_status_label = status_label
creator_action_label = action_label
pipeline_projection = pipeline_stage_projections


def _safe_creator_text(value: Any, *, default: str = "", limit: int = 360) -> str:
    """Keep service explanations readable while hiding engineering tokens."""

    if value is None:
        return default
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()
    if not text:
        return default
    # Common service vocabulary has useful meaning for creators; translate it
    # before removing remaining enum-like identifiers.
    replacements = (
        ("Story revision", "故事版本"),
        ("Story draft", "故事草稿"),
        ("Script draft", "剧本草稿"),
        ("Shot Plan draft", "分镜草稿"),
        ("Production readiness", "视频制作准备度"),
        ("ProductionJob", "视频制作任务"),
        ("ProductionShot", "镜头"),
        ("Provider execution", "AI 生成任务"),
        ("Technical QC", "技术质检"),
        ("Human Review", "人工审片"),
        ("Final Assembly", "成片合成"),
        ("AUTO Mode", "AI 制作台"),
        ("AUTO action", "自动步骤"),
        ("formal", "正式"),
    )
    for source, target in replacements:
        text = text.replace(source, target)
    # URLs and absolute paths are engineering diagnostics, not creator copy.
    # Redact them before the generic enum/hash pass so a blocking explanation
    # can never leak a provider endpoint into the normal workbench surface.
    _value_stop = r"[^\s,;，。；：！？、（）【】《》“”‘’<>\"']+"
    text = re.sub(rf"(?i)\b(?:https?|wss?)://{_value_stop}", "相关链接", text)
    text = re.sub(
        rf"(?i)\b(?:endpoint|host|url)\s*[:=]\s*{_value_stop}",
        "相关配置",
        text,
    )
    # Durable decisions/events may carry identifiers in a human-readable
    # ``why`` string (for example ``provider_task_id=...``).  Those values are
    # intentionally useful to diagnostics but must not leak into the normal
    # creator surface.  Keep this list key-oriented rather than redacting every
    # arbitrary ``*_id`` word, so ordinary copy such as a project title remains
    # intact.  The value class stops at whitespace/punctuation and therefore
    # leaves following Chinese prose untouched.
    internal_key_pattern = (
        r"(?:provider[_ -]?(?:task[_ -]?)?(?:id|uuid)|"
        r"task[_ -]?(?:id|uuid)|execution[_ -]?(?:id|uuid)|"
        r"artifact[_ -]?(?:id|uuid)|asset[_ -]?(?:id|uuid)|"
        r"job[_ -]?(?:id|uuid)|shot[_ -]?(?:id|uuid)|"
        r"revision[_ -]?(?:id|uuid)|project[_ -]?(?:id|uuid)|"
        r"resume[_ -]?(?:token|id)|manifest[_ -]?(?:id|hash|sha256)|"
        r"input[_ -]?state[_ -]?hash|authorization[_ -]?fingerprint|"
        r"source[_ -]?sha256|(?:sha256|endpoint|host|url)|"
        r"absolute[_ -]?path|codec[_ -]?(?:id|name)|actor)"
    )
    text = re.sub(
        rf"(?i)\b{internal_key_pattern}\b\s*[:=]\s*{_value_stop}",
        "相关记录",
        text,
    )
    # Also hide a bare diagnostic key (for example when a service emits
    # ``resume_token`` without an attached value) rather than exposing the
    # implementation vocabulary in creator copy.
    text = re.sub(rf"(?i)\b{internal_key_pattern}\b", "相关记录", text)
    text = re.sub(r"(?i)(?:[a-z]:[\\/]|/)(?:[^\s]+[\\/])+[^\s]*", "相关文件", text)
    text = re.sub(r"\b[0-9a-f]{32,}\b", "相关记录", text, flags=re.IGNORECASE)
    # Do not put raw enum values in the normal explanation.  Preserve ordinary
    # Chinese/English words and punctuation.
    text = re.sub(r"\b[A-Z][A-Z0-9_]{3,}\b", "当前步骤", text)
    return text[:limit]


def _blocking_code(decision: Any) -> str:
    raw = _field(decision, "blocking_reason", default="")
    return _enum_text(raw)


def _is_uncertain_create(decision: Any) -> bool:
    code = _blocking_code(decision)
    return any(
        marker in code
        for marker in (
            "UNCERTAIN_CREATE",
            "RECONCILIATION_REQUIRED",
            "SUBMISSION_UNCERTAIN",
        )
    )


def _human_route(decision: Any) -> tuple[str, str, str, str]:
    requested = _enum_text(_field(decision, "requested_action", default=""))
    stage = _enum_text(_field(decision, "current_stage", default=""))
    if requested in _HUMAN_ROUTES:
        cta, route = _HUMAN_ROUTES[requested]
        completed = _HUMAN_COMPLETED.get(requested, "当前步骤")
        return requested, cta, route, completed
    if stage in _STAGE_HUMAN_FALLBACKS:
        cta, route, completed = _STAGE_HUMAN_FALLBACKS[stage]
        return requested or "WAITING_HUMAN", cta, route, completed
    return requested or "WAITING_HUMAN", "处理人工操作", "dashboard", "当前步骤"


def dominant_action(decision: Any) -> DominantActionProjection:
    """Return exactly one primary action for the supplied durable decision."""

    status = _enum_text(_field(decision, "status", default=""))
    action = _enum_text(_field(decision, "next_action", default=""))
    why = _safe_creator_text(
        _field(decision, "why", default=""),
        default="当前流程已读取最新项目状态。",
    )

    # Safety gates always outrank ordinary execution controls.
    if (
        _flag(_field(decision, "requires_paid_authorization", default=False))
        or action == "PAID_AUTHORIZATION_REQUIRED"
    ):
        return DominantActionProjection(
            "需要你的授权",
            "视频生成将严格按照下方精确范围创建任务；未确认前不会发起生成请求。",
            "授权并继续",
            "paid",
        )
    # An uncertain provider create is a stricter fail-closed boundary than a
    # generic human gate. Always explain that no second submission will be
    # made, even when an older state row also carries ``requires_human=True``.
    if _is_uncertain_create(decision):
        title, reason, cta, route = _BLOCKING_LABELS["UNCERTAIN_CREATE"]
        return DominantActionProjection(title, reason, cta, "uncertain", route)
    if (
        _flag(_field(decision, "requires_human", default=False))
        or status == "WAITING_HUMAN"
        or action == "WAITING_HUMAN"
    ):
        _requested, cta, route, completed = _human_route(decision)
        reason = (
            f"AI 已完成{completed}。正式确认后，AI 制作台会从当前流程继续。"
            f" {_safe_creator_text(why)}"
        )
        return DominantActionProjection("需要你的确认", reason, cta, "human", route)
    # A malformed/future action must never become a button that calls
    # ``resume``.  Keep the fallback creator-friendly and route-only so the
    # user can leave the page while the durable diagnostic remains available.
    if action not in _KNOWN_ACTIONS:
        return DominantActionProjection(
            "下一步需要确认",
            "当前步骤暂时无法安全识别；项目内容未被修改，请查看高级诊断或返回工作台。",
            "返回工作台",
            "unknown",
            "dashboard",
        )
    if status not in _KNOWN_STATUSES:
        return DominantActionProjection(
            "状态需要确认",
            "当前项目状态暂时无法安全识别；项目内容未被修改，请查看高级诊断或返回工作台。",
            "返回工作台",
            "unknown",
            "dashboard",
        )
    if status == "WAITING_PROVIDER":
        return DominantActionProjection(
            "AI 正在生成",
            "任务状态已保存，可以安全离开页面；返回后会从原任务继续。",
            "检查最新进度",
            "provider",
        )
    if status == "SUCCEEDED":
        return DominantActionProjection(
            "制作完成",
            "目标时长与当前项目流程已完成，可以查看最终成片。",
            "查看成片",
            "success",
            "postproduction",
        )
    if status == "CANCELLED":
        return DominantActionProjection(
            "制作已取消",
            "项目内容保持不变；你可以返回工作台选择其他项目操作。",
            "返回工作台",
            "cancelled",
            "dashboard",
        )
    if status in {"BLOCKED", "FAILED"}:
        code = _blocking_code(decision)
        if code in _BLOCKING_LABELS:
            title, reason, cta, route = _BLOCKING_LABELS[code]
            return DominantActionProjection(title, reason, cta, "blocked", route)
        requested, cta, route, _completed = _human_route(decision)
        if requested not in _HUMAN_ROUTES and requested not in _STAGE_HUMAN_FALLBACKS:
            cta, route = "检查失败原因", "settings"
        return DominantActionProjection(
            "需要处理",
            _safe_creator_text(why, default="当前步骤尚未完成，请检查后再继续。"),
            cta,
            "blocked",
            route,
        )
    if action == "NONE":
        return DominantActionProjection(
            "无需自动操作",
            "当前流程没有可执行的自动步骤；项目内容未被修改。",
            "返回工作台",
            "route",
            "dashboard",
        )
    # The first AUTO action is the workbench entry point.  Keep the action
    # mapping itself precise (``生成故事`` remains the semantic label), while
    # presenting a friendlier first-fold CTA to a brand-new project.
    if (
        status == "IDLE"
        and action == "GENERATE_OR_CREATE_STORY"
        and not (_field(decision, "completed_stages", default=()) or ())
    ):
        return DominantActionProjection("开始创作", why, "开始自动制作", "run")
    if status == "RUNNING":
        return DominantActionProjection("正在推进", why, "继续自动制作", "run")
    return DominantActionProjection("下一步", why, action_label(action), "run")


def _navigate(page_key: str) -> None:
    from aidrama_studio.components.navigation import request_navigation

    request_navigation(page_key)


def _run(service: AutoOrchestratorService, project_id: str, *, one_step: bool) -> None:
    """Invoke the existing orchestrator only after an explicit click."""

    try:
        state = service.step(project_id) if one_step else service.resume(project_id)
    except Exception:
        st.error("AI 制作台未能执行当前步骤；详细原因已保留在高级诊断中。")
        return
    if state is not None and _enum_text(_field(state, "status", default="")) == "FAILED":
        st.error("当前步骤未完成，请检查阻塞原因后再继续。")
        return
    st.rerun()


def _duration_text(project: Any) -> str:
    raw = _field(
        project,
        "target_duration_seconds",
        "target_episode_duration_seconds",
        default=None,
    )
    if raw is None:
        profile = _field(project, "output_profile", "profile", default=None)
        raw = _field(
            profile,
            "target_episode_duration_seconds",
            "target_duration_seconds",
            default=None,
        )
    if raw is None:
        return "未设置"
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        text = str(raw).strip()
        return f"{text} 秒" if text else "未设置"
    if numeric <= 0:
        return "未设置"
    rendered = str(int(numeric)) if numeric.is_integer() else f"{numeric:g}"
    return f"{rendered} 秒"


def _aspect_text(project: Any) -> str:
    raw = _field(project, "aspect_ratio", default=None)
    if raw is None:
        return "未设置"
    value = getattr(raw, "value", raw)
    text = str(value).strip()
    return text or "未设置"


def _project_title(project: Any) -> str:
    title = str(_field(project, "title", default="当前项目") or "当前项目").strip()
    title = re.sub(r"[\x00-\x1f\x7f]", " ", title)
    return title[:120] or "当前项目"


def _render_hero(project: Any, decision: Any) -> None:
    title = escape(_project_title(project))
    duration = escape(_duration_text(project))
    aspect = escape(_aspect_text(project))
    stage = escape(
        _STAGE_LABELS.get(
            _enum_text(_field(decision, "current_stage", default="")),
            "当前阶段",
        )
    )
    status_value = status_label(_field(decision, "status", default=""))
    status = escape(status_value)
    st.markdown(
        # Use the creator-facing status in the DOM marker as well as in the
        # visible copy; raw ``WAITING_PROVIDER``/``SUCCEEDED`` enums belong to
        # diagnostics, not the ordinary workbench surface.
        f'<section class="aidrama-auto-hero" data-status="{status}">'
        '<div class="aidrama-auto-hero-copy">'
        '<span class="aidrama-auto-hero-kicker">AI PRODUCTION WORKBENCH</span>'
        '<span class="aidrama-auto-hero-project-label">项目</span>'
        f'<h2>{title}</h2><p>自动制作流程 · 从创意到成片的可追踪进度</p></div>'
        '<div class="aidrama-auto-hero-facts">'
        f'<div><span>目标时长</span><strong>{duration}</strong></div>'
        f'<div><span>画幅</span><strong>{aspect}</strong></div>'
        f'<div><span>当前制作阶段</span><strong>{stage}</strong></div>'
        f'<div><span>状态</span><strong class="aidrama-auto-hero-status">{status}</strong></div>'
        '</div></section>',
        unsafe_allow_html=True,
    )


def _production_progress(service: Any, project_id: str, decision: Any) -> str | None:
    """Read optional shot progress from the canonical current-state service."""

    stage = _enum_text(_field(decision, "current_stage", default=""))
    status = _enum_text(_field(decision, "status", default=""))
    if stage not in {"PRODUCTION", "QC", "REVIEW", "FINAL"} and status != "WAITING_PROVIDER":
        return None
    state_service = _field(service, "current_state_service", default=None)
    derive = getattr(state_service, "derive", None)
    if not callable(derive):
        return None
    try:
        state = derive(project_id)
        shots = tuple(_field(state, "shots", default=()) or ())
    except Exception:
        return None
    if not shots:
        return None
    total = len(shots)
    completed = sum(
        1
        for shot in shots
        if _enum_text(_field(shot, "status", default="")) in {"SUCCEEDED", "SKIPPED"}
    )
    return f"{completed} / {total}"


def _render_progress_summary(service: Any, project_id: str, decision: Any) -> None:
    progress = _production_progress(service, project_id, decision)
    if progress is not None:
        st.metric("视频制作进度", progress)
    elif _enum_text(_field(decision, "status", default="")) == "WAITING_PROVIDER":
        st.caption("视频生成正在后台进行")


def _render_human_gate(decision: Any, projection: DominantActionProjection) -> None:
    with st.container(border=True):
        st.markdown(
            '<section class="aidrama-auto-next-action aidrama-auto-human-gate">'
            '<span class="aidrama-section-kicker">下一步</span>'
            '<h3>需要你的确认</h3></section>',
            unsafe_allow_html=True,
        )
        st.write(projection.reason)
        st.caption("完成正式人工操作后返回 AI 制作台，AUTO 会从已保存的项目状态继续。")
        if st.button(
            projection.cta,
            type="primary",
            key=f"auto-human-{_field(decision, 'project_id', default='project')}",
            use_container_width=True,
        ):
            _navigate(projection.route or "dashboard")
        with st.expander("恢复信息（高级）", expanded=False):
            # The durable token remains owned by the orchestrator.  It is not
            # a creator control and should not be copied, edited, or exposed
            # in the normal workbench surface.
            st.caption("正式人工操作完成后，返回本页即可继续；恢复凭据由本地服务安全管理。")


def _render_paid_gate(
    service: AutoOrchestratorService,
    project_id: str,
    decision: Any,
    projection: DominantActionProjection | None = None,
) -> None:
    projection = projection or dominant_action(decision)
    with st.container(border=True):
        st.markdown(
            '<section class="aidrama-auto-next-action aidrama-auto-paid-gate">'
            '<span class="aidrama-section-kicker">下一步 · 付费授权</span>'
            '<h3>需要你的授权</h3></section>',
            unsafe_allow_html=True,
        )
        st.write("请确认以下精确范围后再继续。AI 制作台不会默认授权或消费预算。")
        try:
            preview = service.preview_paid_authorization(project_id)
        except Exception:
            st.error("无法生成精确授权预览；没有发起生成请求。")
            return
        try:
            required_count = _strict_int(
                _field(preview, "required_create_count", default=None), minimum=1
            )
            per_item_max = _strict_int(
                _field(preview, "per_item_max", default=None), minimum=1
            )
            retry_limit = _strict_int(
                _field(preview, "retry_limit", default=None), minimum=0
            )
            fingerprint = str(
                _field(preview, "authorization_fingerprint", default="") or ""
            ).strip()
        except (TypeError, ValueError):
            st.error("无法确认精确授权范围；没有发起生成请求。")
            return
        if per_item_max != 1 or retry_limit != 0 or not re.fullmatch(
            r"[0-9a-f]{64}", fingerprint, flags=re.IGNORECASE
        ):
            st.error("无法确认精确授权范围；没有发起生成请求。")
            return
        left, middle, right = st.columns(3)
        left.metric("预计创建", required_count)
        middle.metric("单项上限", per_item_max)
        right.metric("自动重试", retry_limit)
        confirmed = st.checkbox(
            "我确认仅授权以上精确范围",
            value=False,
            key=f"auto-paid-confirm-{project_id}-{fingerprint}",
        )
        if st.button(
            projection.cta,
            type="primary",
            disabled=not confirmed,
            key=f"auto-paid-grant-{project_id}",
            use_container_width=True,
        ):
            try:
                service.grant_paid_authorization(
                    project_id,
                    authorization_fingerprint=fingerprint,
                    global_max=required_count,
                    per_item_max=per_item_max,
                    retry_limit=retry_limit,
                )
            except Exception:
                st.error("授权预览已失效，请刷新后重新确认。没有发起生成请求。")
                return
            st.success("有界授权已保存；尚未发起生成请求。")
            st.rerun()


def _render_provider_wait(
    decision: Any,
    projection: DominantActionProjection,
    service: Any,
    project_id: str,
) -> None:
    with st.container(border=True):
        st.markdown(
            '<section class="aidrama-auto-next-action aidrama-auto-provider-wait">'
            '<span class="aidrama-section-kicker">下一步</span>'
            '<h3>AI 正在生成</h3></section>',
            unsafe_allow_html=True,
        )
        st.write(projection.reason)
        _render_progress_summary(service, project_id, decision)
        if st.button(
            projection.cta,
            type="primary",
            key=f"auto-provider-poll-{project_id}",
            use_container_width=True,
        ):
            _run(service, project_id, one_step=True)


def _render_success(
    project: Any,
    projection: DominantActionProjection,
    project_id: str,
) -> None:
    with st.container(border=True):
        st.markdown(
            '<section class="aidrama-auto-success">'
            '<span class="aidrama-section-kicker">AI PRODUCTION WORKBENCH</span>'
            '<h2>制作完成</h2>'
            '<p>流程完成，最终成片已经准备好。</p></section>',
            unsafe_allow_html=True,
        )
        cols = st.columns(2)
        with cols[0]:
            st.metric("项目", _project_title(project))
        with cols[1]:
            st.metric("目标时长", _duration_text(project))
        if st.button(
            projection.cta,
            type="primary",
            key=f"auto-success-final-{project_id}",
            use_container_width=True,
        ):
            _navigate("postproduction")
        if st.button(
            "查看制作详情",
            type="secondary",
            key=f"auto-success-production-{project_id}",
            use_container_width=True,
        ):
            _navigate("production")


def _render_blocked(
    decision: Any,
    projection: DominantActionProjection,
    project_id: str,
) -> None:
    with st.container(border=True):
        marker = (
            "aidrama-auto-uncertain"
            if projection.mode == "uncertain"
            else "aidrama-auto-blocked"
        )
        st.markdown(
            f'<section class="aidrama-auto-next-action {marker}">'
            '<span class="aidrama-section-kicker">下一步</span>'
            f'<h3>{escape(projection.title)}</h3></section>',
            unsafe_allow_html=True,
        )
        st.warning(projection.reason)
        if st.button(
            projection.cta,
            type="primary",
            key=f"auto-blocked-{project_id}",
            use_container_width=True,
        ):
            _navigate(projection.route or "production")


def _render_idle_action(
    service: Any,
    decision: Any,
    projection: DominantActionProjection,
    project_id: str,
) -> None:
    with st.container(border=True):
        st.markdown(
            '<section class="aidrama-auto-next-action">'
            '<span class="aidrama-section-kicker">下一步</span>'
            f'<h3>{escape(projection.title)}</h3></section>',
            unsafe_allow_html=True,
        )
        st.write(projection.reason)
        if st.button(
            projection.cta,
            type="primary",
            key=f"auto-run-{project_id}",
            use_container_width=True,
        ):
            _run(service, project_id, one_step=False)
        # A collapsed compatibility affordance keeps bookmarks/tests from the
        # pre-workbench shell functional without competing with the single
        # dominant first-fold action.
        if projection.cta == "开始自动制作":
            with st.expander("旧版入口（高级）", expanded=False):
                if st.button(
                    "开始 / 继续自动制作",
                    type="secondary",
                    key=f"auto-legacy-run-{project_id}",
                    use_container_width=True,
                ):
                    _run(service, project_id, one_step=False)


def _render_cancelled(projection: DominantActionProjection, project_id: str) -> None:
    with st.container(border=True):
        st.info(projection.reason)
        if st.button(
            projection.cta,
            type="primary",
            key=f"auto-cancelled-{project_id}",
            use_container_width=True,
        ):
            _navigate(projection.route or "dashboard")


def _render_unknown(projection: DominantActionProjection, project_id: str) -> None:
    """Render a route-only fallback for an unknown/future service action."""

    with st.container(border=True):
        st.markdown(
            '<section class="aidrama-auto-next-action aidrama-auto-blocked">'
            '<span class="aidrama-section-kicker">下一步</span>'
            f'<h3>{escape(projection.title)}</h3></section>',
            unsafe_allow_html=True,
        )
        st.warning(projection.reason)
        if st.button(
            projection.cta,
            type="primary",
            key=f"auto-unknown-{project_id}",
            use_container_width=True,
        ):
            _navigate(projection.route or "dashboard")


def _capability_key(snapshot: Any) -> str:
    raw = _field(snapshot, "capability", "kind", "key", default="")
    return _enum_text(raw)


def _capability_display_state(snapshot: Any) -> str:
    value = _field(snapshot, "display_state", default=None)
    if value:
        display = str(value).strip()
        # The canonical ``CapabilitySnapshot`` already supplies one of these
        # labels.  Whitelist it here as a second boundary for injected/legacy
        # adapters so a provider name, URL, or model identifier cannot become
        # normal-page copy.
        if display in _CAPABILITY_STATE_LABELS:
            return display
    ready = _field(snapshot, "ready", default=None)
    if ready is True:
        return "已配置"
    state = _enum_text(_field(snapshot, "state", "status", default=""))
    return {
        "NEEDS_SETUP": "需要配置",
        "NEEDS_VERIFICATION": "待验证",
        "UNAVAILABLE": "暂不可用",
        "ERROR": "配置有误",
        "NEEDS_CONFIRMATION": "需要确认",
    }.get(state, "需要配置")


def render_capability_summary(
    project_id: str | None = None,
    *,
    snapshots: Any = None,
) -> tuple[Any, ...]:
    """Render the five provider-neutral creator capabilities (read-only)."""

    try:
        normalized = normalize_capability_snapshots(
            snapshots,
            project_id=project_id,
        )
    except TypeError:
        # A tiny compatibility seam for injected test adapters that only accept
        # the project keyword.
        normalized = normalize_capability_snapshots(project_id=project_id)
    except Exception:
        normalized = ()

    by_key = {_capability_key(item): item for item in normalized}
    cards: list[str] = []
    missing_critical = False
    for key, label in CAPABILITY_DEFINITIONS:
        canonical = {
            "text": "LLM",
            "image": "IMAGE",
            "video": "VIDEO",
            "vision": "VISION",
            "tts": "TTS",
        }[key]
        snapshot = by_key.get(canonical)
        state_text = (
            _capability_display_state(snapshot)
            if snapshot is not None
            else "需要配置"
        )
        ready = (
            bool(_field(snapshot, "ready", default=False))
            if snapshot is not None
            else False
        )
        if canonical in {"LLM", "VIDEO"} and not ready:
            missing_critical = True
        css_state = "ready" if ready else "missing"
        cards.append(
            f'<div class="aidrama-auto-capability-card aidrama-auto-capability-{css_state}" '
            f'data-state="{escape(css_state)}">'
            f'<span>{escape(_CAPABILITY_LABELS.get(canonical, label))}</span>'
            f'<strong>{escape(state_text)}</strong></div>'
        )

    with st.container(border=True):
        st.markdown(
            '<section class="aidrama-auto-capabilities">'
            '<span class="aidrama-section-kicker">AI READINESS</span>'
            '<h3>AI 能力</h3>'
            f'<div class="aidrama-auto-capability-grid">{"".join(cards)}</div>'
            '</section>',
            unsafe_allow_html=True,
        )
        if missing_critical:
            st.info("视频制作需要准备创作 AI 与视频生成能力。")
            if st.button(
                "去设置 AI 模型",
                type="secondary",
                key=f"auto-capability-settings-{project_id or 'global'}",
                use_container_width=True,
            ):
                _navigate("settings")
    return tuple(normalized)


def _render_advanced(decision: Any, persisted: Any, events: list[Any] | Any) -> None:
    try:
        event_items = list(events or ())
    except (TypeError, ValueError):
        event_items = []
    with st.expander("高级 · AI 决策记录", expanded=False):
        st.markdown('<span class="aidrama-auto-advanced-marker"></span>', unsafe_allow_html=True)
        st.caption("持久化决策记录仅供排障；普通创作无需查看。")
        if not event_items:
            st.caption("AI 制作台尚未记录事件。")
        for event in reversed(event_items[-20:]):
            action = action_label(_field(event, "action", default=""))
            result = _safe_creator_text(
                _field(event, "result", default=""), default="已记录"
            )
            reason = _safe_creator_text(
                _field(event, "reason", default=""), default="已保存当前决策。"
            )
            timestamp = _safe_creator_text(
                _field(event, "timestamp", default=""), default=""
            )
            st.markdown(
                f"**{escape(action)}** · {escape(result)}  \n"
                f"{escape(reason)}"
                + (f"  \n{escape(timestamp)}" if timestamp else "")
            )
        # Internal hashes, provider references, and resume tokens stay in the
        # durable service/diagnostic boundary.  The workbench only needs the
        # safe event projection above, even when this disclosure is opened.
        if persisted is not None or decision is not None:
            st.caption("完整技术字段保留在本地诊断记录中。")


def render() -> None:
    project = current_project_or_stop()
    page_header(
        "AI 制作台",
        "AI PRODUCTION WORKBENCH",
        "从创意到成片，清楚看到 AI 正在做什么、为什么停下，以及下一步需要你的哪项确认。",
        stage="AI 制作台",
    )

    service = AutoOrchestratorService(drive_background=True)
    project_id = str(_field(project, "id", default=""))
    try:
        # Reads are deliberately isolated from all action handlers.  A
        # transient database/runtime read failure must fail closed without
        # inventing a client-side status or attempting a provider call.
        decision = service.next_action(project_id)
    except Exception:
        st.error("暂时无法读取 AI 制作台状态；项目内容未被修改，请稍后重试。")
        render_capability_summary(project_id)
        _render_advanced(None, None, [])
        return
    try:
        persisted = service.get_state(project_id)
    except Exception:
        persisted = None
    input_hash = _field(decision, "input_state_hash", default=None)
    persisted_hash = _field(persisted, "input_state_hash", default=None)
    display = (
        persisted
        if persisted is not None and input_hash and input_hash == persisted_hash
        else decision
    )

    # Keep the canonical project context in the shell; it remains read-only and
    # does not add a competing CTA on this page.
    render_project_context(project, suppress_next=True)
    _render_hero(project, decision)

    # Put the state-driven next action directly after the hero.  At the target
    # 1366x768 workstation size the creator can act without scrolling, while
    # the detailed ten-stage rail and secondary metrics remain immediately
    # below for orientation.
    projection = dominant_action(decision)
    mode = projection.mode
    if mode == "paid":
        _render_paid_gate(service, project_id, decision, projection)
    elif mode == "human":
        _render_human_gate(display, projection)
    elif mode == "provider":
        _render_provider_wait(decision, projection, service, project_id)
    elif mode == "success":
        _render_success(project, projection, project_id)
    elif mode in {"blocked", "uncertain"}:
        _render_blocked(decision, projection, project_id)
    elif mode == "cancelled":
        _render_cancelled(projection, project_id)
    elif mode in {"unknown", "route"}:
        _render_unknown(projection, project_id)
    else:
        _render_idle_action(service, decision, projection, project_id)

    try:
        keyframe_readiness = service.keyframe_readiness(project_id)
    except Exception:
        keyframe_readiness = None
    render_pipeline(decision, keyframe_readiness=keyframe_readiness)

    cols = st.columns(3)
    cols[0].metric(
        "当前阶段",
        _STAGE_LABELS.get(
            _enum_text(_field(decision, "current_stage", default="")),
            "当前阶段",
        ),
    )
    cols[1].metric("当前状态", status_label(_field(decision, "status", default="")))
    cols[2].metric("下一步", action_label(_field(decision, "next_action", default="")))

    # Optional shot count comes from CurrentProductionState only.  It is
    # omitted when the canonical service cannot provide a reliable projection.
    if mode not in {"provider"}:
        _render_progress_summary(service, project_id, decision)

    render_capability_summary(project_id)

    if _enum_text(_field(decision, "status", default="")) not in {
        "SUCCEEDED",
        "CANCELLED",
    }:
        with st.expander("更多操作", expanded=False):
            if st.button(
                "取消自动制作",
                type="secondary",
                key=f"auto-cancel-{project_id}",
                use_container_width=True,
            ):
                try:
                    service.cancel(project_id, reason="user_cancelled_from_auto_ui")
                except Exception:
                    st.error("无法取消当前流程；项目内容保持不变。")
                    return
                st.rerun()

    try:
        events = service.list_events(project_id)
    except Exception:
        events = []
    _render_advanced(decision, persisted, events)


__all__ = [
    "DominantActionProjection",
    "AutoAction",
    "AutoOrchestratorError",
    "AutoRunStatus",
    "PIPELINE_DEFINITIONS",
    "PipelineStageProjection",
    "_ACTION_LABELS",
    "_HUMAN_ROUTES",
    "_STAGE_LABELS",
    "_STATUS_LABELS",
    "action_label",
    "creator_action_label",
    "dominant_action",
    "human_status_label",
    "pipeline_stage_projections",
    "pipeline_projection",
    "render",
    "render_capability_summary",
    "status_label",
]
