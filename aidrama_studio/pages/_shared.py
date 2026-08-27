"""Shared shell and projection helpers for the AIDrama workspaces.

The page modules deliberately stay thin: workflow state is projected by
``CurrentProductionStateService`` and the shell translates that projection to
human-facing labels. Capability and activity helpers consume a small,
provider-neutral shape so a future universal runtime adapter can be wired in
without changing every page.

The compatibility readiness adapter at the bottom of this module is kept
behind one function. It is only used when no neutral snapshot is supplied and
can be removed once the universal runtime publishes its public projection.
Nothing from that adapter is rendered as provider/task metadata in normal
mode.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from html import escape
import math
import re
from typing import Any

import streamlit as st
from loguru import logger

from aidrama_studio.components.empty_state import empty_state
from aidrama_studio.domain import ProjectStatus
from aidrama_studio.services import ProjectService
from aidrama_studio.services.current_state import CurrentProductionStateService


# ---------------------------------------------------------------------------
# Canonical workflow projection
# ---------------------------------------------------------------------------

# These labels are the product IA. They intentionally do not mirror the
# compatibility enum names (for example, DRAFT is presented as 创意).
STAGE_LABELS: dict[ProjectStatus, str] = {
    ProjectStatus.DRAFT: "创意",
    ProjectStatus.STORY: "故事 / 剧本",
    ProjectStatus.PREPRODUCTION: "分镜",
    ProjectStatus.PRODUCTION: "制作",
    ProjectStatus.REVIEW: "审片",
    ProjectStatus.POSTPRODUCTION: "成片",
    ProjectStatus.COMPLETED: "成片",
}

STAGE_NEXT: dict[ProjectStatus, tuple[str, str]] = {
    ProjectStatus.DRAFT: ("开始创作", "creative"),
    ProjectStatus.STORY: ("继续故事 / 剧本", "story"),
    ProjectStatus.PREPRODUCTION: ("检查并确认分镜", "director"),
    ProjectStatus.PRODUCTION: ("查看制作进度", "production"),
    ProjectStatus.REVIEW: ("完成审片", "review"),
    ProjectStatus.POSTPRODUCTION: ("生成最终成片", "postproduction"),
    ProjectStatus.COMPLETED: ("播放成片", "postproduction"),
}


@dataclass(frozen=True, slots=True)
class WorkflowStageProjection:
    """Safe shell projection of the canonical workflow stage.

    ``canonical`` is false only when the canonical service could not be read.
    In that case V1 uses the audited, safe Creative fallback and exposes a
    non-technical diagnostic beside the context banner. The project row's
    compatibility ``status`` is never consulted as a fallback authority.
    """

    status: ProjectStatus
    label: str
    next_action: str
    next_page: str
    canonical: bool = True
    diagnostic: str | None = None

    @property
    def route(self) -> str:
        """Alias used by callers that call the destination a route."""

        return self.next_page


def _coerce_project_status(value: Any) -> ProjectStatus | None:
    """Coerce enum-like values without exposing or trusting raw enum text."""

    if isinstance(value, ProjectStatus):
        return value
    candidate = getattr(value, "value", value)
    if candidate is None:
        return None
    try:
        return ProjectStatus(str(candidate).strip().upper())
    except (TypeError, ValueError):
        return None


def _project_id(project_or_id: Any) -> str | None:
    # Accept both repository models and small mapping/test doubles.  A mapping
    # must contribute its actual id rather than its stringified representation.
    value = _field(project_or_id, "id", default=project_or_id)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def workflow_stage_projection(
    project_or_id: Any,
    *,
    state_service: CurrentProductionStateService | Any | None = None,
    current_state_service: CurrentProductionStateService | Any | None = None,
) -> WorkflowStageProjection:
    """Read the canonical workflow stage for a project.

    ``state_service`` and ``current_state_service`` are injectable seams for
    deterministic page tests and for the eventual universal runtime adapter.
    They are aliases so existing callers can migrate without a flag day.
    """

    service = (
        current_state_service
        if current_state_service is not None
        else state_service
    )
    project_id = _project_id(project_or_id)
    if service is None:
        service = CurrentProductionStateService()

    if project_id:
        try:
            status = _coerce_project_status(service.workflow_stage(project_id))
            if status is not None:
                action, page = STAGE_NEXT.get(status, ("继续创作", "creative"))
                return WorkflowStageProjection(
                    status=status,
                    label=STAGE_LABELS.get(status, "创意"),
                    next_action=action,
                    next_page=page,
                )
            logger.warning(
                "canonical workflow stage was not recognized for project {}",
                project_id,
            )
        except Exception:
            # A malformed legacy project or a transient read failure must not
            # make the whole creative workspace disappear. Keep the fallback
            # explicit so callers can show a diagnostic and never silently
            # switch to ``project.status``.
            logger.exception(
                "failed to derive canonical workflow stage for project {}",
                project_id,
            )
    else:
        logger.warning("cannot derive canonical workflow stage without a project id")

    fallback = ProjectStatus.DRAFT
    action, page = STAGE_NEXT[fallback]
    return WorkflowStageProjection(
        status=fallback,
        label=STAGE_LABELS[fallback],
        next_action=action,
        next_page=page,
        canonical=False,
        diagnostic="当前阶段暂时无法读取，已保留创意入口；稍后可重新打开项目。",
    )


# Descriptive aliases make the contract discoverable to page owners while
# keeping the original ``project_stage`` helper compatible with older pages.
canonical_workflow_stage = workflow_stage_projection
workflow_projection = workflow_stage_projection


def canonical_stage(
    project: Any,
    *,
    state_service: CurrentProductionStateService | Any | None = None,
    current_state_service: CurrentProductionStateService | Any | None = None,
) -> ProjectStatus:
    """Return only the canonical enum for code that needs state branching."""

    return workflow_stage_projection(
        project,
        state_service=state_service,
        current_state_service=current_state_service,
    ).status


current_workflow_stage = canonical_stage
get_canonical_stage = canonical_stage


def project_stage(
    project: Any,
    *,
    state_service: CurrentProductionStateService | Any | None = None,
    current_state_service: CurrentProductionStateService | Any | None = None,
) -> str:
    """Return the canonical, human-readable stage label for ``project``."""

    return workflow_stage_projection(
        project,
        state_service=state_service,
        current_state_service=current_state_service,
    ).label


def get_project_service() -> ProjectService:
    return ProjectService()


def _navigate(page_key: str) -> None:
    from aidrama_studio.components.navigation import request_navigation

    request_navigation(page_key)


def render_project_context(
    project: Any,
    *,
    # ``stage`` remains accepted for source compatibility. It is deliberately
    # ignored as a state authority; canonical service output always wins.
    stage: str | None = None,
    next_action: str | None = None,
    next_page: str | None = None,
    quiet: bool | None = None,
    suppress_next: bool = False,
    projection: WorkflowStageProjection | None = None,
    state_service: CurrentProductionStateService | Any | None = None,
    current_state_service: CurrentProductionStateService | Any | None = None,
) -> WorkflowStageProjection:
    """Render the persistent project/stage/next-action shell.

    A page may provide a local next action as a secondary affordance, but the
    displayed stage is always obtained from ``CurrentProductionStateService``.
    The return value lets page code reuse the same projection without another
    repository read.
    """

    projection = projection or workflow_stage_projection(
        project,
        state_service=state_service,
        current_state_service=current_state_service,
    )
    # A page-supplied action denotes a local dominant CTA.  In that case the
    # shell affordance is automatically quiet unless the caller explicitly
    # opts into a prominent button.  This keeps the first fold to one primary
    # action while preserving the old API for callers with no overrides.
    if quiet is None:
        quiet = next_action is not None or next_page is not None
    action = next_action or projection.next_action
    page = next_page or projection.next_page
    project_title = escape(str(getattr(project, "title", "当前项目")))
    stage_label = escape(projection.label)
    state_class = "canonical" if projection.canonical else "degraded"
    diagnostic = (
        f'<span class="aidrama-context-diagnostic">{escape(projection.diagnostic)}</span>'
        if projection.diagnostic
        else ""
    )
    st.markdown(
        f'<section class="aidrama-workspace-context {state_class}" '
        f'data-stage="{escape(projection.status.value.lower())}">'
        f'<div><span class="aidrama-context-kicker">当前项目</span>'
        f'<strong>{project_title}</strong></div>'
        f'<div><span class="aidrama-context-kicker">当前阶段</span>'
        f'<strong>{stage_label}</strong>{diagnostic}</div></section>',
        unsafe_allow_html=True,
    )
    if not suppress_next:
        action_col, status_col = st.columns([2, 5])
        with action_col:
            if st.button(
                action,
                type="secondary" if quiet else "primary",
                key=f"workspace-next-{getattr(project, 'id', 'project')}-{page}",
                use_container_width=True,
            ):
                _navigate(page)
        with status_col:
            st.caption("下一步 · " + action)
    return projection


# ---------------------------------------------------------------------------
# Provider-neutral capability and background activity contracts
# ---------------------------------------------------------------------------

CAPABILITY_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("text", "文本生成"),
    ("image", "参考图生成"),
    ("video", "视频生成"),
    ("vision", "画面分析"),
    ("tts", "配音"),
)

AUTOMATION_MODES: tuple[str, str] = ("协作模式", "连续生成")
AUTOMATION_STOP_GATES: tuple[str, ...] = (
    "故事 / 剧本确认",
    "参考锁定",
    "付费视频任务",
    "人工审片通过",
    "最终导出 / 交付",
)


def render_automation_mode(
    project_id: str | None = None,
    *,
    compact: bool = False,
) -> str:
    """Render the explicit creator automation mode and its hard stop gates.

    This is a UI preference only.  It never submits work or infers approval;
    the production/runtime services remain responsible for enforcing each
    durable gate.  Collaboration is deliberately the safe default.
    """

    key = f"aidrama-automation-mode-{project_id or 'global'}"
    current = st.session_state.get(key, AUTOMATION_MODES[0])
    if current not in AUTOMATION_MODES:
        current = AUTOMATION_MODES[0]
    selected = st.radio(
        "自动化方式",
        list(AUTOMATION_MODES),
        index=list(AUTOMATION_MODES).index(current),
        horizontal=not compact,
        key=key,
        help="协作模式默认每一步都由你确认；连续生成只会创建非付费草稿。",
    )
    if selected == "连续生成":
        st.caption(
            "连续生成只创建非付费草稿；到达故事 / 剧本确认、参考锁定、付费任务、"
            "人工审片或最终导出前会自动停下，等待你的确认。"
        )
    else:
        st.caption("协作模式：每个关键步骤都由你确认后才会继续。")
    return selected

_CAPABILITY_ALIASES: dict[str, str] = {
    # Universal runtime values are canonical uppercase identifiers. The
    # lowercase spellings are UI-friendly aliases used by fixture authors.
    "text": "LLM",
    "llm": "LLM",
    "image": "IMAGE",
    "images": "IMAGE",
    "video": "VIDEO",
    "video_generative": "VIDEO",
    "vision": "VISION",
    "tts": "TTS",
    "voice": "TTS",
}

_CAPABILITY_INTERNAL_KEYS: dict[str, str] = {
    "LLM": "text",
    "IMAGE": "image",
    "VIDEO": "video",
    "VISION": "vision",
    "TTS": "tts",
}


def _capability_key(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    canonical = _CAPABILITY_ALIASES.get(str(raw or "").strip().casefold())
    return _CAPABILITY_INTERNAL_KEYS.get(canonical or "")


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        try:
            result = getattr(value, name)
        except AttributeError:
            continue
        return result
    return default


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if value is True or value is False:
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1", "on", "ready", "available"}:
            return True
        if normalized in {"false", "no", "0", "off", "unavailable", "error"}:
            return False
    return default


def _safe_text(
    value: Any,
    *,
    default: str | None = None,
    limit: int = 240,
) -> str | None:
    if value is None:
        return default
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()
    if not text:
        return default
    # Keep normal copy free of absolute paths, hashes, and worker/provider
    # identifiers even when a legacy adapter supplies an unsanitized detail.
    text = re.sub(
        r"(?i)(?:[a-z]:[\\/]|/)(?:[^\s]+[\\/])+[^\s]*",
        "相关文件",
        text,
    )
    text = re.sub(r"\b[0-9a-f]{32,}\b", "相关记录", text, flags=re.IGNORECASE)
    return text[:limit]


def _safe_model_label(value: Any) -> str | None:
    """Extract a display label without surfacing provider/endpoint identity."""

    if isinstance(value, Mapping):
        value = value.get("display_name") or value.get("label") or value.get("name") or value.get("model_or_profile")
    else:
        value = (
            getattr(value, "display_name", None)
            or getattr(value, "label", None)
            or getattr(value, "name", None)
            or value
        )
    return _safe_text(value, limit=120)


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Provider-neutral capability readiness projection.

    Only ``model_or_profile`` is a safe display label. Provider IDs,
    endpoints, credentials, and runtime plans are intentionally absent.
    """

    capability: str
    model_or_profile: str | None = None
    configured: bool = False
    verified: bool = False
    runtime_available: bool = False
    create_authorized: bool = False
    authorization_required: bool = False
    safe_reason: str | None = None
    # Compatibility state is retained solely to distinguish a known malformed
    # configuration from an ordinary unavailable capability.
    readiness_state: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        capability = str(self.capability or "").strip()
        canonical = _CAPABILITY_ALIASES.get(capability.casefold(), capability.upper())
        object.__setattr__(self, "capability", canonical)
        object.__setattr__(self, "configured", self.configured is True)
        object.__setattr__(self, "verified", self.verified is True)
        object.__setattr__(self, "runtime_available", self.runtime_available is True)
        object.__setattr__(self, "create_authorized", self.create_authorized is True)
        object.__setattr__(self, "authorization_required", self.authorization_required is True)
        object.__setattr__(self, "model_or_profile", _safe_model_label(self.model_or_profile))
        object.__setattr__(self, "safe_reason", _safe_text(self.safe_reason))
        state = _safe_text(self.readiness_state)
        object.__setattr__(self, "readiness_state", state.upper() if state else None)

    @property
    def state(self) -> str:
        """Stable machine-neutral state name for UI logic and tests."""

        if self.readiness_state == "ERROR":
            return "error"
        if not self.configured:
            return "needs_setup"
        if not self.verified:
            return "needs_verification"
        if not self.runtime_available:
            return "unavailable"
        if self.authorization_required and not self.create_authorized:
            return "needs_confirmation"
        return "ready"

    @property
    def status(self) -> str:
        """Alias for callers that use status terminology."""

        return self.state

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    @property
    def display_state(self) -> str:
        return {
            "ready": "已配置",
            "needs_setup": "需要配置",
            "needs_verification": "待验证",
            "unavailable": "运行不可用",
            "needs_confirmation": "需要确认",
            "error": "配置有误",
        }.get(self.state, "需要配置")

    def as_public_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "model_or_profile": self.model_or_profile,
            "configured": self.configured,
            "verified": self.verified,
            "runtime_available": self.runtime_available,
            "create_authorized": self.create_authorized,
            "authorization_required": self.authorization_required,
            "safe_reason": self.safe_reason,
            "state": self.state,
        }


def capability_snapshot_fixture(
    *,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[CapabilitySnapshot, ...]:
    """Build a deterministic offline fixture for page/AppTest coverage.

    The fixture is never selected implicitly for production rendering; callers
    must pass it to ``render_capability_cards`` or install it through the
    explicit session seam.
    """

    overrides = overrides or {}
    result: list[CapabilitySnapshot] = []
    for key, _label in CAPABILITY_DEFINITIONS:
        values = dict(overrides.get(key, {}))
        values.setdefault("capability", key)
        result.append(CapabilitySnapshot(**values))
    return tuple(result)


CAPABILITY_SNAPSHOT_FIXTURE = capability_snapshot_fixture()
DEFAULT_CAPABILITY_SNAPSHOTS = CAPABILITY_SNAPSHOT_FIXTURE


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    """Durable/background activity projection consumed by the shell.

    Progress is optional and is never fabricated by the UI. ``activity_id``
    is retained for action keys but is not displayed in normal mode.
    """

    activity_id: str | None = None
    title: str = ""
    detail: str | None = None
    state: str = "idle"
    progress: float | None = None
    next_action: str | None = None
    next_page: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "activity_id", _safe_text(self.activity_id, limit=96))
        object.__setattr__(
            self,
            "title",
            _safe_text(self.title, default="后台活动", limit=160) or "后台活动",
        )
        object.__setattr__(self, "detail", _safe_text(self.detail, limit=240))
        object.__setattr__(self, "next_action", _safe_text(self.next_action, limit=80))
        object.__setattr__(self, "next_page", _safe_text(self.next_page, limit=48))
        object.__setattr__(self, "updated_at", _safe_text(self.updated_at, limit=80))
        state = (_safe_text(self.state, default="idle", limit=32) or "idle").casefold()
        aliases = {
            "pending": "queued",
            "in_progress": "running",
            "complete": "ready",
            "completed": "ready",
            "succeeded": "ready",
            "canceled": "cancelled",
        }
        object.__setattr__(self, "state", aliases.get(state, state))
        progress = self.progress
        if isinstance(progress, bool):
            progress = None
        try:
            progress = float(progress) if progress is not None else None
        except (TypeError, ValueError):
            progress = None
        if progress is not None and math.isfinite(progress):
            object.__setattr__(self, "progress", max(0.0, min(1.0, progress)))
        else:
            object.__setattr__(self, "progress", None)

    @property
    def visible(self) -> bool:
        return self.state not in {"idle", "cancelled", "hidden"}

    @property
    def is_active(self) -> bool:
        return self.state in {"queued", "running", "paused", "interrupted"}

    def as_public_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "detail": self.detail,
            "state": self.state,
            "progress": self.progress,
            "next_action": self.next_action,
            "next_page": self.next_page,
            "updated_at": self.updated_at,
        }


def normalize_capability_snapshot(
    value: Any,
    *,
    capability: str | None = None,
) -> CapabilitySnapshot:
    """Normalize a mapping/object supplied by any runtime adapter."""

    raw_capability = _field(
        value,
        "capability",
        "kind",
        "key",
        default=capability or "",
    )
    raw_capability = getattr(raw_capability, "value", raw_capability)
    canonical = _CAPABILITY_ALIASES.get(
        str(raw_capability or "").casefold(), str(raw_capability or "").upper()
    )
    raw_state = _field(value, "state", "status", default=None)
    raw_state = getattr(raw_state, "value", raw_state)
    state = str(raw_state or "").strip().upper() or None
    ready_raw = _field(value, "ready", default=None)
    ready_present = ready_raw is not None
    ready = _bool_value(ready_raw)
    configured_raw = _field(value, "configured", default=None)
    verified_raw = _field(value, "verified", default=None)
    runtime_raw = _field(value, "runtime_available", "available", default=None)
    explicit_dimensions = any(
        item is not None for item in (configured_raw, verified_raw, runtime_raw)
    )
    auth_required_raw = _field(
        value,
        "authorization_required",
        "requires_create_authorization",
        "requires_authorization",
        default=None,
    )
    authorized_raw = _field(
        value,
        "create_authorized",
        "authorized",
        "live_authorized",
        default=None,
    )
    # Legacy readiness records expose only ``ready`` and a state. Expand that
    # shape into the neutral dimensions while keeping malformed READY records
    # fail-closed.
    if configured_raw is None:
        configured_raw = ready if ready_present else state == "READY"
    if verified_raw is None:
        verified_raw = ready if ready_present else state == "READY"
    if runtime_raw is None:
        runtime_raw = ready if ready_present else state == "READY"
    if state == "READY" and not ready_present:
        # A bare READY marker is not proof of all three independent facts.
        # Only an explicit true value for each dimension can produce a green
        # card when the universal adapter omits its convenience ``ready`` bit.
        configured_raw = configured_raw is True
        verified_raw = verified_raw is True
        runtime_raw = runtime_raw is True
    if auth_required_raw is None:
        auth_required_raw = _field(value, "create_is_paid", default=False)
    auth_required = _bool_value(auth_required_raw)
    if authorized_raw is None:
        authorized_raw = ready if ready_present else (not auth_required and state == "READY")

    # Keep legacy convenience state and the independent dimensions consistent.
    # A contradictory payload must fail closed instead of becoming a green
    # card merely because one boolean happens to be true.  ``CONFIGURED`` and
    # other adapter-specific informational states are left advisory; the
    # neutral dimensions remain authoritative for those records.
    configured = _bool_value(configured_raw)
    verified = _bool_value(verified_raw)
    runtime_available = _bool_value(runtime_raw)
    dimensions_ready = configured and verified and runtime_available
    if state == "READY":
        if (
            (ready_present and ready is not True)
            or (explicit_dimensions and not dimensions_ready)
            or (not ready_present and not explicit_dimensions)
        ):
            state = "ERROR"
    elif state == "UNAVAILABLE":
        if (ready_present and ready is True) or (
            explicit_dimensions and dimensions_ready
        ):
            state = "ERROR"
    elif state == "ERROR":
        # Preserve the explicit error regardless of any stale true flags.
        state = "ERROR"
    elif state and state not in {
        # Informational states emitted by the legacy profile/runtime bridges
        # are advisory; the independent dimensions below remain authoritative.
        "CONFIGURED",
        "AVAILABLE",
        "VERIFIED",
        "NOT_VERIFIED",
        "UNVERIFIED",
        "PENDING",
        "NEEDS_SETUP",
        "NEEDS_VERIFICATION",
        "NEEDS_CONFIRMATION",
        "RUNTIME_UNAVAILABLE",
    }:
        # Unknown state + any payload is unsafe to interpret as ready.  Mark
        # it malformed while retaining the raw value only in Advanced data.
        state = "ERROR"
    if state == "ERROR":
        runtime_raw = False
    return CapabilitySnapshot(
        capability=canonical,
        model_or_profile=_safe_model_label(_field(
            value,
            "model_or_profile",
            "model",
            "profile",
            default=None,
        )),
        configured=configured,
        verified=verified,
        runtime_available=runtime_available if state != "ERROR" else False,
        create_authorized=_bool_value(authorized_raw),
        authorization_required=auth_required,
        safe_reason=_field(value, "safe_reason", "reason", "detail", default=None),
        readiness_state=state,
    )


def _call_source(source: Any, *, project_id: str | None = None) -> Any:
    if source is None:
        return _default_capability_source(project_id)
    if callable(source):
        for invocation in (
            lambda: source(project_id=project_id),
            lambda: source(project_id),
            lambda: source(),
        ):
            try:
                return invocation()
            except TypeError:
                continue
    for method_name in ("snapshot", "capability_snapshot", "list_capabilities"):
        method = getattr(source, method_name, None)
        if not callable(method):
            continue
        for invocation in (
            lambda: method(project_id=project_id),
            lambda: method(project_id),
            lambda: method(),
        ):
            try:
                return invocation()
            except TypeError:
                continue
    return source


def _default_capability_source(project_id: str | None = None) -> Any:
    """Resolve a neutral source, with a narrow legacy compatibility seam."""

    injected = st.session_state.get("_aidrama_capability_source")
    if injected is not None:
        return _call_source(injected, project_id=project_id)
    injected_snapshot = st.session_state.get("_aidrama_capability_snapshots")
    if injected_snapshot is not None:
        return injected_snapshot
    try:
        # Import lazily so normal page modules depend only on this neutral
        # contract. The legacy service performs no network calls and is a
        # temporary bridge until the universal runtime projection lands.
        from aidrama_studio.services.provider_readiness import ProviderReadinessService

        return ProviderReadinessService().snapshot(project_id=project_id)
    except Exception:
        logger.exception("failed to read capability readiness projection")
        return {}


def set_capability_source(source: Any) -> None:
    """Install an explicit neutral capability adapter for the current UI session."""

    st.session_state["_aidrama_capability_source"] = source


def clear_capability_source() -> None:
    st.session_state.pop("_aidrama_capability_source", None)


def normalize_capability_snapshots(
    source: Any = None,
    *,
    project_id: str | None = None,
) -> tuple[CapabilitySnapshot, ...]:
    """Return exactly the five creator-facing capability cards in IA order."""

    payload = _call_source(source, project_id=project_id)
    by_key: dict[str, Any] = {}
    if _field(payload, "capability", "kind", default=None) is not None:
        raw = _field(payload, "capability", "kind", default="")
        raw = getattr(raw, "value", raw)
        normalized_key = _capability_key(raw)
        if normalized_key:
            by_key[normalized_key] = payload
    elif isinstance(payload, Mapping):
        for key, value in payload.items():
            normalized_key = _capability_key(key)
            if normalized_key:
                by_key[normalized_key] = value
            elif _field(value, "capability", "kind", default=None) is not None:
                raw = _field(value, "capability", "kind", default="")
                raw = getattr(raw, "value", raw)
                normalized_key = _capability_key(raw)
                if normalized_key:
                    by_key[normalized_key] = value
    elif isinstance(payload, Iterable) and not isinstance(payload, (str, bytes)):
        for value in payload:
            raw = _field(value, "capability", "kind", "key", default="")
            raw = getattr(raw, "value", raw)
            normalized_key = _capability_key(raw)
            if normalized_key:
                by_key[normalized_key] = value

    snapshots: list[CapabilitySnapshot] = []
    for key, _label in CAPABILITY_DEFINITIONS:
        value = by_key.get(
            key,
            {"capability": key, "state": "UNAVAILABLE", "configured": False},
        )
        snapshots.append(normalize_capability_snapshot(value, capability=key))
    return tuple(snapshots)


# Short alias for page owners and the future runtime adapter.
capability_snapshots = normalize_capability_snapshots


def render_capability_cards(
    snapshots: Any = None,
    *,
    project_id: str | None = None,
    compact: bool = False,
    show_diagnostics: bool = True,
) -> tuple[CapabilitySnapshot, ...]:
    """Render creator-facing capability status without provider internals."""

    normalized = normalize_capability_snapshots(snapshots, project_id=project_id)
    missing = [
        label
        for snapshot, (_key, label) in zip(normalized, CAPABILITY_DEFINITIONS)
        if not snapshot.ready
    ]
    with st.container(border=True):
        st.markdown(
            '<section class="aidrama-capability-panel">'
            '<div class="aidrama-section-kicker">创作能力</div>'
            '<h3>AI 能力状态</h3></section>',
            unsafe_allow_html=True,
        )
        cols = st.columns(len(CAPABILITY_DEFINITIONS))
        for col, snapshot, (_key, label) in zip(
            cols, normalized, CAPABILITY_DEFINITIONS
        ):
            with col:
                status_class = snapshot.state.replace("_", "-")
                st.markdown(
                    f'<div class="aidrama-capability-card aidrama-capability-{escape(status_class)}">'
                    f'<span class="aidrama-capability-label">{escape(label)}</span>'
                    f'<span class="aidrama-capability-state">{escape(snapshot.display_state)}</span></div>',
                    unsafe_allow_html=True,
                )
                # Keep metrics for Streamlit accessibility/testing and for
                # compact layouts where the card HTML may be collapsed.
                st.metric(label, snapshot.display_state)
        if missing:
            st.info(
                "部分 AI 能力还未准备好。你仍可先编辑内容；需要生成时再完成对应设置。"
            )
            if st.button(
                "去设置模型",
                # Capability setup is a utility escape hatch.  The page's
                # state-dependent creative action remains the single dominant
                # CTA in the first fold.
                type="secondary",
                key=f"readiness-settings-{project_id or 'global'}",
                use_container_width=not compact,
            ):
                _navigate("settings")
        if show_diagnostics:
            with st.expander("高级诊断", expanded=False):
                st.caption("仅供排障使用；普通创作不需要理解运行配置或任务细节。")
                st.json([snapshot.as_public_dict() for snapshot in normalized])
    return normalized


def render_ai_readiness(
    *,
    project_id: str | None = None,
    compact: bool = False,
    snapshots: Any = None,
    source: Any = None,
) -> tuple[CapabilitySnapshot, ...]:
    """Backward-compatible entry point for capability cards."""

    return render_capability_cards(
        snapshots if snapshots is not None else source,
        project_id=project_id,
        compact=compact,
    )


def normalize_activity_snapshot(value: Any) -> ActivitySnapshot:
    if isinstance(value, ActivitySnapshot):
        return value
    state = _field(value, "state", "status", default="idle")
    state = getattr(state, "value", state)
    return ActivitySnapshot(
        activity_id=_field(value, "activity_id", "id", "job_id", default=None),
        title=_field(value, "title", "label", "name", default="后台活动"),
        detail=_field(value, "detail", "message", "description", default=None),
        state=str(state or "idle"),
        progress=_field(value, "progress", "fraction", default=None),
        next_action=_field(value, "next_action", "action_label", default=None),
        next_page=_field(value, "next_page", "route", "page", default=None),
        updated_at=_field(value, "updated_at", "created_at", default=None),
    )


def normalize_activity_snapshots(source: Any = None) -> tuple[ActivitySnapshot, ...]:
    if source is None:
        source = st.session_state.get("_aidrama_activity_snapshots", ())
    if callable(source):
        try:
            source = source()
        except TypeError:
            source = source(project_id=st.session_state.get("current_project_id"))
    if isinstance(source, Mapping) and any(
        key in source for key in ("state", "status", "title", "label", "message")
    ):
        values: Sequence[Any] = (source,)
    elif isinstance(source, Mapping):
        values: Sequence[Any] = tuple(source.values())
    elif isinstance(source, Iterable) and not isinstance(source, (str, bytes)):
        values = tuple(source)
    elif source:
        values = (source,)
    else:
        values = ()
    return tuple(
        item
        for item in (normalize_activity_snapshot(value) for value in values)
        if item.visible
    )


ACTIVITY_SNAPSHOT_FIXTURE: tuple[ActivitySnapshot, ...] = ()
DEFAULT_ACTIVITY_SNAPSHOTS = ACTIVITY_SNAPSHOT_FIXTURE


def set_activity_source(source: Any) -> None:
    """Install an explicit neutral activity adapter for the current UI session."""

    st.session_state["_aidrama_activity_snapshots"] = source


def clear_activity_source() -> None:
    st.session_state.pop("_aidrama_activity_snapshots", None)


activity_snapshots = normalize_activity_snapshots


def render_background_activity(
    activities: Any = None,
    *,
    compact: bool = False,
) -> tuple[ActivitySnapshot, ...]:
    """Render non-blocking background activity while keeping the workspace mounted."""

    normalized = normalize_activity_snapshots(activities)
    if not normalized:
        return ()
    for index, activity in enumerate(normalized):
        state_class = escape(activity.state.replace("_", "-"))
        progress = ""
        if activity.progress is not None:
            progress = (
                f'<div class="aidrama-activity-progress" role="progressbar" '
                f'aria-valuemin="0" aria-valuemax="1" '
                f'aria-valuenow="{activity.progress:.3f}">'
                f'<span style="width:{activity.progress * 100:.1f}%"></span></div>'
            )
        detail = f"<span>{escape(activity.detail)}</span>" if activity.detail else ""
        st.markdown(
            f'<section class="aidrama-activity-strip aidrama-activity-{state_class}" '
            f'data-activity-state="{state_class}">'
            '<span class="aidrama-activity-orb" aria-hidden="true"></span>'
            f'<div class="aidrama-activity-copy"><strong>{escape(activity.title)}</strong>{detail}</div>'
            f"{progress}</section>",
            unsafe_allow_html=True,
        )
        if activity.next_action and activity.next_page:
            key_id = activity.activity_id or str(index)
            if st.button(
                activity.next_action,
                key=f"activity-next-{key_id}-{index}",
                use_container_width=not compact,
            ):
                _navigate(activity.next_page)
    return normalized


# Aliases used by pages with slightly different vocabulary.
render_activity_strip = render_background_activity
render_ai_activity = render_background_activity
BackgroundActivitySnapshot = ActivitySnapshot
ActivityRecord = ActivitySnapshot


# ---------------------------------------------------------------------------
# Human blocker and project recovery helpers
# ---------------------------------------------------------------------------


def render_actionable_blockers(
    blockers: list[str] | tuple[str, ...] | None,
    *,
    project_id: str | None = None,
) -> None:
    """Translate machine gates into direct, human actions."""

    blockers = [str(item) for item in (blockers or [])]
    if not blockers:
        return
    normalized = " ".join(blockers).lower()
    items: list[tuple[str, str, str]] = []
    if "story" in normalized or "故事" in normalized:
        items.append(
            ("确认故事设定", "先确认故事设定，后续角色与剧本才能保持一致。", "story")
        )
    if "script" in normalized or "剧本" in normalized:
        items.append(("确认剧本", "确认结构化剧本后，才能进入分镜。", "story"))
    if "shot" in normalized or "分镜" in normalized:
        items.append(("确认分镜", "确认镜头顺序和时长后，才能开始制作。", "director"))
    if "reference" in normalized or "asset" in normalized or "参考" in normalized:
        items.append(("补齐参考图", "为主要角色和场景选择或上传参考图。", "assets"))
    if not items:
        items.append(("查看当前准备项", "完成准备清单后即可继续。", "creative"))
    with st.container(border=True):
        st.markdown("### 当前还不能继续")
        st.caption("还需要完成：")
        for title, detail, page in items:
            left, right = st.columns([4, 1])
            left.markdown(f"○ **{title}**")
            left.caption(detail)
            if right.button(
                "去处理",
                key=f"blocker-{project_id or 'project'}-{page}-{title}",
                use_container_width=True,
            ):
                _navigate(page)


def current_project_or_stop():
    project_id = st.session_state.get("current_project_id")
    if not project_id:
        empty_state(
            "未选择项目",
            "请选择最近项目继续，或创建一个新项目。",
            label="WORKSPACE / 需要项目",
        )
        back_col, create_col = st.columns(2)
        if back_col.button(
            "返回工作台选择项目", type="primary", use_container_width=True
        ):
            _navigate("dashboard")
        if create_col.button("创建新项目", use_container_width=True):
            _navigate("dashboard")
        try:
            recent = get_project_service().list()[:3]
        except Exception:
            recent = []
        if recent:
            st.markdown("#### 最近项目")
            for item in recent:
                if st.button(
                    f"继续 · {item.title}",
                    key=f"recover-{item.id}",
                    use_container_width=True,
                ):
                    st.session_state.current_project_id = item.id
                    st.query_params["project"] = item.id
                    st.rerun()
        st.stop()
    # Keep deep links/reloads project-scoped even when Streamlit's navigation
    # component rewrites the path without carrying query parameters.
    if st.query_params.get("project") != project_id:
        st.query_params["project"] = project_id
    try:
        project = get_project_service().get(project_id)
    except Exception:
        logger.exception("failed to load current AIDrama project")
        st.error("项目读取失败，请返回工作台后重试。")
        st.stop()
    if project is None:
        st.session_state.current_project_id = None
        st.warning("当前项目已经不存在，请重新选择项目。")
        if st.button("返回工作台", type="primary"):
            from aidrama_studio.components.navigation import request_navigation

            request_navigation("dashboard")
        st.stop()
    return project


def coming_soon(title: str, description: str, future_items: list[str]) -> None:
    project = current_project_or_stop()
    st.caption(f"当前项目 · {project.title}")
    empty_state(title, description, label="COMING SOON / 当前阶段尚未开始")
    st.markdown("#### 后续将在这里完成")
    for item in future_items:
        st.markdown(f"- {item}")


__all__ = [
    "ActivitySnapshot",
    "ActivityRecord",
    "BackgroundActivitySnapshot",
    "ACTIVITY_SNAPSHOT_FIXTURE",
    "AUTOMATION_MODES",
    "AUTOMATION_STOP_GATES",
    "CAPABILITY_DEFINITIONS",
    "CAPABILITY_SNAPSHOT_FIXTURE",
    "CapabilitySnapshot",
    "DEFAULT_ACTIVITY_SNAPSHOTS",
    "DEFAULT_CAPABILITY_SNAPSHOTS",
    "STAGE_LABELS",
    "STAGE_NEXT",
    "WorkflowStageProjection",
    "activity_snapshots",
    "canonical_workflow_stage",
    "canonical_stage",
    "capability_snapshots",
    "coming_soon",
    "current_project_or_stop",
    "get_project_service",
    "get_canonical_stage",
    "normalize_activity_snapshot",
    "normalize_activity_snapshots",
    "normalize_capability_snapshot",
    "normalize_capability_snapshots",
    "project_stage",
    "render_actionable_blockers",
    "render_automation_mode",
    "render_activity_strip",
    "render_ai_activity",
    "render_ai_readiness",
    "render_background_activity",
    "render_capability_cards",
    "render_project_context",
    "set_activity_source",
    "set_capability_source",
    "clear_activity_source",
    "clear_capability_source",
    "capability_snapshot_fixture",
    "workflow_projection",
    "workflow_stage_projection",
    "current_workflow_stage",
]
