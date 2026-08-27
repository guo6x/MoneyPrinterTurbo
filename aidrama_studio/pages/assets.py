from __future__ import annotations

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.domain import (
    ReferenceAssetType,
    ReferenceBindingType,
    ReferenceImageCandidateStatus,
)
from aidrama_studio.pages._shared import current_project_or_stop, render_project_context
from aidrama_studio.services import (
    CreativeIntakeService,
    ReferenceAgentError,
    ReferenceAgentService,
    ReferenceAssetService,
    ReferenceAssetServiceError,
    ReferenceAssetStorageError,
    ReferenceAssetStorageService,
)
from aidrama_studio.services.security import sanitize_error


_VERSION_STATUS_LABELS = {
    "NO REFERENCE": "未添加参考",
    "REFERENCE OUTDATED": "故事设定已更新",
    "LOCKED": "已锁定参考",
    "DRAFT": "参考草稿",
}
_TIME_LABELS = {
    "DAY": "白天",
    "NIGHT": "夜晚",
    "DAWN": "黎明",
    "DUSK": "黄昏",
    "UNSPECIFIED": "时间未指定",
}


def _value(item, name: str, default=None):
    """Read a field from either a service model or a small fixture mapping."""

    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _enum_value(value):
    """Normalize enum-like and plain-string values at the UI boundary."""

    return getattr(value, "value", value)


def _binding_is(value, expected: ReferenceBindingType) -> bool:
    return str(_enum_value(value) or "").strip().upper() == expected.value


def _candidate_status(value) -> str:
    return str(_enum_value(value) or "").strip().upper()


def _human_version_status(status: str) -> str:
    return _VERSION_STATUS_LABELS.get(status, "待处理")


def _safe_error(exc: object, *, fallback: str = "操作未完成") -> str:
    """Keep normal-page failures concise and free of paths/diagnostic payloads."""

    detail = sanitize_error(exc, max_length=160)
    return detail or fallback


def _brief(subject, binding_type: ReferenceBindingType) -> str:
    if _binding_is(binding_type, ReferenceBindingType.CHARACTER):
        return (
            " · ".join(
                str(value)
                for value in (
                    _value(subject, "identity"),
                    _value(subject, "appearance"),
                    _value(subject, "personality"),
                )
                if value
            )
            or "暂无视觉摘要"
        )
    return (
        " · ".join(
                _TIME_LABELS.get(str(_enum_value(value)), str(value))
            for value in (
                _value(subject, "environment"),
                _value(subject, "visual_style"),
                _value(subject, "time_of_day"),
            )
            if value
        )
        or "暂无视觉摘要"
    )


def _outdated(version, story_revision_id: str) -> bool:
    metadata = _value(version, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return bool(
        version and metadata.get("source_story_revision_id") != story_revision_id
    )


def _version_status(asset, version, story_revision_id: str) -> str:
    if version is None:
        return "NO REFERENCE"
    if _outdated(version, story_revision_id):
        return "REFERENCE OUTDATED"
    if _value(asset, "current_version_id") == _value(version, "id"):
        return "LOCKED"
    return "DRAFT"


def _asset_status(service, project, binding_type, subject_id, story_revision_id):
    finder = getattr(service, "find_workspace_asset", None)
    asset = (
        finder(project.id, binding_type, subject_id)
        if callable(finder)
        else service.find_asset_for_binding(project.id, binding_type, subject_id)
    )
    current = (
        service.get_current_version(project.id, _value(asset, "id"))
        if asset
        else None
    )
    return asset, current, _version_status(asset, current, story_revision_id)


def _enqueue_image_activity(
    project,
    *,
    subject,
    binding_type,
    story_revision_id,
    prompt,
    create_authorized: bool = False,
):
    """Queue image generation through an optional runtime-owned adapter.

    Normal page rendering never instantiates an image runtime or calls a
    provider.  Integrations can install a small callable in session state;
    source mode gets an honest pending/unavailable state instead.
    """
    adapter = st.session_state.get("_aidrama_activity_adapter")
    payload = {
        "subject_id": _value(subject, "id"),
        "binding_type": _enum_value(binding_type),
        "source_story_revision_id": story_revision_id,
        "prompt": prompt,
        "create_authorized": create_authorized is True,
    }
    if not callable(adapter):
        st.session_state[f"assets-activity-{project.id}"] = {
            "state": "pending",
            "message": "参考图后台生成能力尚未连接；上传、比较和锁定仍可继续。",
        }
        return None
    try:
        result = adapter(
            project_id=project.id,
            operation="REFERENCE_IMAGE_CANDIDATE",
            payload=payload,
        )
    except TypeError:
        result = adapter(project.id, "REFERENCE_IMAGE_CANDIDATE", payload)
    except Exception as exc:
        st.session_state[f"assets-activity-{project.id}"] = {
            "state": "failed",
            "message": _safe_error(exc, fallback="后台活动暂未完成"),
        }
        return None
    st.session_state[f"assets-activity-{project.id}"] = {
        "state": "ready",
        "message": "真实候选图已安全保存；请比较后再提升并锁定。",
    }
    return result


def _render_asset_activity(project) -> None:
    activity = st.session_state.get(f"assets-activity-{project.id}")
    if not isinstance(activity, dict):
        return
    state = str(activity.get("state") or "pending")
    message = str(activity.get("message") or "参考图活动状态待同步。")
    if state in {"pending", "queued", "running"}:
        st.info(message)
    elif state == "failed":
        st.warning("参考图活动暂未完成：" + message)


def _readiness(service, project, subjects, binding_type, story_revision_id):
    key = (
        "characters"
        if _binding_is(binding_type, ReferenceBindingType.CHARACTER)
        else "locations"
    )
    stats = service.calculate_readiness(project.id, story_revision_id)[key]
    return int(stats["locked"]), list(stats["missing_names"])


def _render_autonomous_reference_status(project, service) -> None:
    """Show the read-only Agent projection without creating any provider work."""

    try:
        agent = ReferenceAgentService(
            service.repository,
            reference_assets=service,
        )
        readiness = agent.reference_readiness(project.id)
    except (AttributeError, ReferenceAgentError, ReferenceAssetServiceError, ValueError, KeyError):
        # The existing workspace remains usable if historical data has not yet
        # reached an exact approved Story -> Script -> Shot Plan chain.
        return
    if readiness.blocked:
        st.caption("自动参考检查等待完整的已确认 Story / Script / Shot Plan。")
        return
    character_total = sum(
        item.subject_type.value == "CHARACTER" for item in readiness.required
    )
    location_total = sum(
        item.subject_type.value == "LOCATION" for item in readiness.required
    )
    satisfied = len(readiness.covered)
    pending_generation = len(readiness.missing)
    st.markdown("### 自动参考检查")
    st.caption(
        f"系统识别到：角色 {character_total} · 场景 {location_total} · "
        f"已满足 {satisfied} · 待生成 {pending_generation}"
    )
    waiting_human = [
        item
        for item in readiness.next_actions
        if item.kind.value == "WAITING_HUMAN_REFERENCE_APPROVAL"
    ]
    if waiting_human:
        st.warning(f"已有 {len(waiting_human)} 个候选图：WAITING_HUMAN")
    elif readiness.stale:
        st.warning(f"{len(readiness.stale)} 个已锁定参考需要人工复核。")
    elif pending_generation:
        st.info("缺失参考已规划；需显式付费授权后才会生成候选图。")
    else:
        st.success("当前已锁定参考满足已确认分镜的生产覆盖。")


def _render_card(service, project, subject, binding_type, story_revision_id):
    asset, current, status = _asset_status(
        service, project, binding_type, _value(subject, "id"), story_revision_id
    )
    with st.container(border=True):
        st.markdown(f"### {_value(subject, 'name', '未命名对象')}")
        st.caption(_brief(subject, binding_type))
        if status == "LOCKED":
            st.success(
                f"已锁定参考 · 第 {_value(current, 'version_number', '—')} 版"
            )
        elif status == "REFERENCE OUTDATED":
            st.warning("故事设定已更新 · 请检查参考是否仍适用")
        elif asset:
            st.info("参考草稿 · 尚未锁定")
        else:
            st.caption("未添加参考")
        if current is not None:
            st.caption(
                f"当前参考 · 第 {_value(current, 'version_number', '—')} 版 · "
                f"{_value(current, 'filename', '未命名图片')}"
            )
            try:
                image_path = service.resolve_version_path(
                    project.id, _value(current, "id")
                )
            except (ReferenceAssetServiceError, OSError, ValueError, KeyError, TypeError):
                image_path = None
            try:
                exists = image_path is not None and image_path.exists()
            except (OSError, ValueError, TypeError):
                exists = False
            if exists:
                st.image(str(image_path), width=180)
        with st.expander("高级来源信息", expanded=False):
            st.caption(f"内部绑定类型 · {_enum_value(binding_type)}")
            st.caption(f"内部对象 ID · {_value(subject, 'id', '—')}")


def _ensure_asset(service, project, binding_type, subject_id):
    finder = getattr(service, "find_workspace_asset", None)
    asset = (
        finder(project.id, binding_type, subject_id)
        if callable(finder)
        else service.find_asset_for_binding(project.id, binding_type, subject_id)
    )
    if asset is None:
        ensure = getattr(service, "ensure_workspace_asset", None)
        if callable(ensure):
            asset = ensure(project.id, binding_type, subject_id)
        else:
            asset_type = (
                ReferenceAssetType.CHARACTER_REFERENCE
                if _binding_is(binding_type, ReferenceBindingType.CHARACTER)
                else ReferenceAssetType.LOCATION_REFERENCE
            )
            asset = service.create_asset(project.id, asset_type)
    return asset


def _generate_image_candidate(
    service,
    runtime,
    project,
    subject,
    binding_type,
    story_revision_id,
    prompt,
):
    """Run the canonical generate/validate/record boundary for the UI."""

    asset = _ensure_asset(service, project, binding_type, _value(subject, "id"))
    binding_value = str(_enum_value(binding_type) or "REFERENCE").upper()
    subject_id = _value(subject, "id")
    candidate = runtime.generate_and_record_candidate(
        project.id,
        _value(asset, "id"),
        prompt,
        source_story_revision_id=story_revision_id,
        filename=f"{binding_value.lower()}-{subject_id}-generated.png",
        metadata={
            "subject_id": subject_id,
            "binding_type": binding_value,
        },
        actor="user",
        reference_assets=service,
    )
    return asset, candidate


def _import_uploads(
    service, storage, project, subject, binding_type, story_revision_id, uploads
) -> int:
    """Import selected files as draft versions and bind each to its Story Bible subject."""
    asset = _ensure_asset(service, project, binding_type, _value(subject, "id"))
    imported = 0
    for upload in uploads:
        try:
            version = storage.import_image(
                project.id,
                _value(asset, "id"),
                upload.getvalue(),
                filename=upload.name,
                mime_type=upload.type or "",
                metadata={
                    "source_story_revision_id": story_revision_id,
                    "subject_id": _value(subject, "id"),
                },
            )
            service.bind_version(
                project.id,
                _value(version, "id"),
                binding_type,
                _value(subject, "id"),
            )
            imported += 1
        except (
            ReferenceAssetStorageError,
            ReferenceAssetServiceError,
            ValueError,
        ) as exc:
            st.warning(f"{upload.name}：{_safe_error(exc, fallback='文件无法作为参考草稿导入')}")
    return imported


def _render_generated_candidates(
    service,
    project,
    asset,
    subject,
    binding_type,
    key_suffix: str = "",
) -> None:
    list_method = getattr(service, "list_image_candidates", None)
    if not callable(list_method):
        return
    try:
        candidates = list(list_method(project.id, _value(asset, "id")))
    except (ReferenceAssetServiceError, OSError, ValueError, KeyError, TypeError) as exc:
        st.warning(f"候选图暂时无法读取：{_safe_error(exc)}")
        return
    st.markdown("### 候选对比")
    st.caption(
        "生成候选 ≠ 参考草稿 ≠ 已锁定参考。提升后仍需单独锁定，AI 结果不会自动进入生产。"
    )
    if not candidates:
        st.info("暂无候选图。可先上传本机图片，或在生成能力可用时请求候选。")
        return
    for start in range(0, len(candidates), 2):
        row = st.columns(2)
        for offset, (column, candidate) in enumerate(
            zip(row, candidates[start : start + 2])
        ):
            with column:
                with st.container(border=True):
                    candidate_status = _value(candidate, "status", "DRAFT")
                    candidate_status_value = _candidate_status(candidate_status)
                    status = {
                        ReferenceImageCandidateStatus.DRAFT: "待选择",
                        ReferenceImageCandidateStatus.PROMOTED: "已提升为参考草稿",
                        ReferenceImageCandidateStatus.REJECTED: "不采用",
                    }.get(
                        candidate_status,
                        {
                            "DRAFT": "待选择",
                            "PROMOTED": "已提升为参考草稿",
                            "REJECTED": "不采用",
                        }.get(str(candidate_status_value), "待处理"),
                    )
                    candidate_id = _value(candidate, "id", f"candidate-{start + offset}")
                    st.markdown(f"**候选 {start + offset + 1}** · {status}")
                    try:
                        candidate_path = service.resolve_image_candidate_path(
                            project.id, _value(candidate, "id")
                        )
                    except (ReferenceAssetServiceError, OSError, ValueError, KeyError, TypeError):
                        candidate_path = None
                    try:
                        candidate_exists = (
                            candidate_path is not None and candidate_path.is_file()
                        )
                    except (OSError, ValueError, TypeError):
                        candidate_exists = False
                    if candidate_exists:
                        st.image(str(candidate_path), use_container_width=True)
                    else:
                        st.error("候选图文件不可用。")
                    with st.expander("高级来源信息", expanded=False):
                        st.caption(
                            f"Provider {getattr(candidate, 'provider_id', '—')} · Model {getattr(candidate, 'model_id', '—')} · "
                            f"Region {getattr(candidate, 'deployment_region', '—')}"
                        )
                        digest = str(getattr(candidate, "sha256", ""))
                        st.caption(
                            f"Candidate ID {getattr(candidate, 'id', '—')} · SHA-256 {digest[:12]}…"
                        )
                        st.write(getattr(candidate, "prompt_text", ""))
                        prompt_digest = str(getattr(candidate, "prompt_sha256", ""))
                        request_digest = str(getattr(candidate, "request_sha256", ""))
                        st.caption(
                            f"Prompt hash {prompt_digest[:12]}… · Request hash {request_digest[:12]}…"
                        )
                        parent_id = getattr(candidate, "parent_candidate_id", None)
                        if parent_id:
                            st.caption(f"Parent candidate · {str(parent_id)[:12]}…")
                    if (
                        candidate_status_value == ReferenceImageCandidateStatus.DRAFT.value
                    ):
                        actions = st.columns(2)
                        if actions[0].button(
                            "提升为参考草稿",
                            key=f"promote-image-candidate-{candidate_id}{key_suffix}",
                        ):
                            try:
                                version = service.promote_image_candidate(
                                    project.id, _value(candidate, "id")
                                )
                                service.bind_version(
                                    project.id,
                                    _value(version, "id"),
                                    binding_type,
                                    _value(subject, "id"),
                                )
                                st.success(
                                    "已提升为参考草稿；尚未锁定，不会自动进入生产。"
                                )
                                st.rerun()
                            except ReferenceAssetServiceError as exc:
                                st.warning(_safe_error(exc, fallback="候选图暂时无法提升"))
                        if actions[1].button(
                            "不采用",
                            key=f"reject-image-candidate-{candidate_id}{key_suffix}",
                        ):
                            try:
                                service.reject_image_candidate(
                                    project.id, _value(candidate, "id")
                                )
                                st.rerun()
                            except ReferenceAssetServiceError as exc:
                                st.warning(_safe_error(exc, fallback="候选图暂时无法标记"))
                    elif (
                        candidate_status_value == ReferenceImageCandidateStatus.PROMOTED.value
                    ):
                        st.success("已进入参考草稿；请预览版本后再明确锁定。")
                    else:
                        st.caption("已标记为不采用；候选历史仍保留。")


def _render_workspace(
    service,
    storage,
    project,
    subject,
    binding_type,
    story_revision_id,
    *,
    key_suffix: str = "",
):
    asset, current, status = _asset_status(
        service, project, binding_type, _value(subject, "id"), story_revision_id
    )
    _render_asset_activity(project)
    subject_id = _value(subject, "id", "subject")
    binding_value = str(_enum_value(binding_type) or "REFERENCE").upper()
    st.markdown(f"## {_value(subject, 'name', '未命名对象')} · 参考详情")
    st.caption(_brief(subject, binding_type))
    if status == "REFERENCE OUTDATED":
        st.warning("故事设定已更新 · 当前参考不会自动替换，请确认是否继续使用。")
    elif status == "LOCKED":
        st.success(
            f"参考已锁定 · 第 {_value(current, 'version_number', '—')} 版"
        )

    st.markdown("### 添加参考草稿")
    st.caption("上传图片会创建新的参考草稿；已锁定版本不会被覆盖。")
    uploads = st.file_uploader(
        "上传 JPEG / PNG / WebP",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        key=f"reference-upload-{project.id}-{binding_value}-{subject_id}{key_suffix}",
    )
    if uploads and st.button(
        "导入为参考草稿",
        type="primary",
        key=f"import-{project.id}-{binding_value}-{subject_id}{key_suffix}",
    ):
        imported = _import_uploads(
            service, storage, project, subject, binding_type, story_revision_id, uploads
        )
        if imported:
            st.success(f"已创建 {imported} 个参考草稿")
            st.rerun()

    st.markdown("### 请求候选图（后台）")
    st.caption("请求会进入后台活动；返回后仍需人工比较、提升和锁定，不会自动采用。")
    generation_prompt = st.text_area(
        "画面描述",
        value=_brief(subject, binding_type),
        height=120,
        key=f"reference-generation-prompt-{project.id}-{binding_value}-{subject_id}{key_suffix}",
    )
    paid_create_authorized = st.checkbox(
        "我确认本次最多创建 1 张付费候选图，且不会自动重试",
        key=f"reference-generation-authorization-{project.id}-{binding_value}-{subject_id}{key_suffix}",
    )
    if st.button(
        "请求生成候选图",
        disabled=not paid_create_authorized,
        key=f"generate-reference-candidate-{project.id}-{binding_value}-{subject_id}{key_suffix}",
    ):
        generated = _enqueue_image_activity(
            project,
            subject=subject,
            binding_type=binding_type,
            story_revision_id=story_revision_id,
            prompt=generation_prompt,
            create_authorized=paid_create_authorized,
        )
        if generated is not None:
            st.rerun()
        _render_asset_activity(project)

    if not asset:
        st.info("暂无参考资产。上传图片或生成候选图后会自动创建。")
        return
    _render_generated_candidates(
        service, project, asset, subject, binding_type, key_suffix
    )
    try:
        versions = service.list_versions(project.id, _value(asset, "id"))
    except (ReferenceAssetServiceError, OSError, ValueError, KeyError, TypeError) as exc:
        st.warning(f"参考版本暂时无法读取：{_safe_error(exc)}")
        return
    st.markdown("### 参考版本")
    for version in reversed(versions):
        version_status = _version_status(asset, version, story_revision_id)
        with st.expander(
            f"第 {_value(version, 'version_number', '—')} 版 · "
            f"{_human_version_status(version_status)} · {_value(version, 'filename', '未命名图片')}",
            expanded=_value(version, "id") == _value(asset, "current_version_id"),
        ):
            try:
                image_path = service.resolve_version_path(project.id, _value(version, "id"))
                image_exists = image_path.exists()
            except (ReferenceAssetServiceError, OSError, ValueError, KeyError, TypeError):
                image_path = None
                image_exists = False
            if image_exists:
                st.image(str(image_path), width=260)
            else:
                st.error("参考图文件不可用。")
            if _value(version, "id") != _value(asset, "current_version_id"):
                st.caption("此版本尚未锁定；锁定后才会用于下游制作。")
                if st.button(
                    "锁定此版本", type="primary", key=f"lock-{_value(version, 'id')}{key_suffix}"
                ):
                    service.activate_version(
                        project.id, _value(asset, "id"), _value(version, "id")
                    )
                    st.success("参考已锁定")
                    st.rerun()
            else:
                st.success("当前锁定版本 · 不可覆盖或删除")
                if st.button(
                    "从锁定版本创建新草稿",
                    key=f"draft-from-locked-{_value(version, 'id')}{key_suffix}",
                ):
                    try:
                        service.create_draft_from_version(
                            project.id,
                            _value(asset, "id"),
                            _value(version, "id"),
                            source_story_revision_id=story_revision_id,
                        )
                        st.success("已创建新的参考草稿")
                        st.rerun()
                    except ReferenceAssetServiceError as exc:
                        st.warning(_safe_error(exc, fallback="无法创建新的参考草稿"))
            if st.checkbox(
                "显示高级版本信息", key=f"advanced-version-{_value(version, 'id')}{key_suffix}"
            ):
                st.caption(
                    f"MIME {_value(version, 'mime_type', '—')} · "
                    f"{_value(version, 'size_bytes', '—')} bytes"
                )
                digest = str(_value(version, "sha256", ""))
                st.caption(
                    f"Version id {_value(version, 'id', '—')} · SHA-256 {digest[:12]}…"
                )


def _render_subject_tab(
    service, storage, project, subjects, binding_type, story_revision_id
):
    if not subjects:
        st.info("当前 Story Bible 没有可用对象。")
        return
    binding_value = str(_enum_value(binding_type) or "REFERENCE").upper()
    key = f"reference-selected-{project.id}-{binding_value}"
    selected_id = st.session_state.get(key, _value(subjects[0], "id"))
    cards = st.columns(min(3, len(subjects)))
    for index, subject in enumerate(subjects):
        with cards[index % len(cards)]:
            _render_card(service, project, subject, binding_type, story_revision_id)
            if st.button(
                "打开详情",
                key=f"open-{project.id}-{binding_value}-{_value(subject, 'id')}",
                use_container_width=True,
            ):
                st.session_state[key] = _value(subject, "id")
                st.rerun()
    selected = next(
        (subject for subject in subjects if _value(subject, "id") == selected_id),
        subjects[0],
    )
    st.divider()
    _render_workspace(
        service, storage, project, selected, binding_type, story_revision_id
    )


def _render_overview_cards(service, project, subjects, binding_type, story_revision_id):
    """Media-first overview cards; detail/lock controls live in other states."""
    if not subjects:
        st.info("暂无可用对象。")
        return
    columns = st.columns(min(3, len(subjects)))
    binding_value = str(_enum_value(binding_type) or "REFERENCE").upper()
    for index, subject in enumerate(subjects):
        with columns[index % len(columns)]:
            _render_card(service, project, subject, binding_type, story_revision_id)
            if st.button(
                "查看详情",
                key=f"overview-open-{project.id}-{binding_value}-{_value(subject, 'id')}",
                use_container_width=True,
            ):
                st.session_state[
                    f"reference-selected-{project.id}-{binding_value}"
                ] = _value(subject, "id")
                st.session_state.assets_workspace = (
                    "角色详情"
                    if _binding_is(binding_type, ReferenceBindingType.CHARACTER)
                    else "场景详情"
                )
                st.rerun()


def _creative_intake_for(service):
    """Build the intake facade against the asset page's repository.

    Source Pack promotion is an Assets concern, but it must still read the
    same project-scoped repository as the surrounding reference service.  A
    small fallback keeps lightweight test doubles that expose a no-argument
    ``CreativeIntakeService`` constructor compatible.
    """

    repository = getattr(service, "repository", None)
    try:
        return (
            CreativeIntakeService(repository)
            if repository is not None
            else CreativeIntakeService()
        )
    except TypeError:
        return CreativeIntakeService()


def _render_source_promotions(project, story_revision, service):
    """Move Source Pack image promotion into Assets with separate lock intent."""
    intake = _creative_intake_for(service)
    try:
        sources = intake.source_pack.list(project.id)
    except Exception:
        return
    image_sources = []
    for item in sources:
        kind = getattr(item, "source_kind", None)
        if str(getattr(kind, "value", kind)) == "IMAGE":
            image_sources.append(item)
    if not image_sources:
        return
    with st.container(border=True):
        st.markdown("### 从本机来源添加参考")
        st.caption(
            "提升只会创建参考草稿；请在候选对比中明确锁定，AI 或导入素材都不会自动锁定。"
        )
        labels = {item.id: item.display_filename for item in image_sources}
        source_id = st.selectbox(
            "来源图片",
            list(labels),
            format_func=lambda value: labels.get(value, "未命名图片"),
            key=f"assets-source-image-{project.id}",
        )
        targets = {
            **{
                f"CHARACTER:{item.id}": f"角色 · {item.name}"
                for item in story_revision["content"].characters
            },
            **{
                f"LOCATION:{item.id}": f"场景 · {item.name}"
                for item in story_revision["content"].locations
            },
        }
        if not targets:
            st.caption("当前故事还没有可绑定的角色或场景。")
            return
        target = st.selectbox(
            "绑定到",
            list(targets),
            format_func=lambda value: targets.get(value, "未命名对象"),
            key=f"assets-source-target-{project.id}",
        )
        if st.button("提升为参考草稿", key=f"assets-promote-source-{project.id}"):
            binding_type, binding_id = target.split(":", 1)
            try:
                intake.promote_image_reference(
                    project.id,
                    source_id,
                    source_story_revision_id=story_revision["id"],
                    binding_type=binding_type,
                    binding_id=binding_id,
                    lock=False,
                )
                st.success("已创建参考草稿；请在候选对比中预览并明确锁定。")
                st.rerun()
            except Exception as exc:
                st.warning(f"提升失败：{_safe_error(exc)}")


def render() -> None:
    page_header(
        "角色与场景",
        "REFERENCE ASSET CENTER",
        "以媒体卡片管理角色、场景和明确锁定的视觉参考。",
    )
    project = current_project_or_stop()
    render_project_context(
        project,
        stage="角色与场景",
        next_action="检查并锁定参考图",
        next_page="director",
    )
    service = ReferenceAssetService()
    storage = ReferenceAssetStorageService(service)
    story_revision = service.approved_story_revision(project.id)
    if story_revision is None:
        st.warning("还不能进入参考图工作区")
        st.caption("请先确认故事设定，确认后这里会自动列出角色和场景。")
        if st.button("去确认故事", type="primary", key=f"assets-story-{project.id}"):
            from aidrama_studio.components.navigation import request_navigation

            request_navigation("story")
        return
    story = story_revision["content"]
    readiness = service.calculate_readiness(project.id, story_revision["id"])
    character_readiness = readiness["characters"]
    location_readiness = readiness["locations"]
    st.markdown("## 资产准备度")
    st.caption("先为主要角色和场景选择或上传参考图，再进入分镜。")
    st.markdown("#### 角色")
    character_metrics = st.columns(4)
    character_metrics[0].metric("角色总数", character_readiness["total"])
    character_metrics[1].metric("已使用", character_readiness["used"])
    character_metrics[2].metric("已锁定", character_readiness["locked"])
    character_metrics[3].metric("待补齐", character_readiness["missing"])
    st.markdown("#### 场景")
    location_metrics = st.columns(4)
    location_metrics[0].metric("场景总数", location_readiness["total"])
    location_metrics[1].metric("已使用", location_readiness["used"])
    location_metrics[2].metric("已锁定", location_readiness["locked"])
    location_metrics[3].metric("待补齐", location_readiness["missing"])
    missing_characters = list(character_readiness["missing_names"])
    missing_locations = list(location_readiness["missing_names"])
    if missing_characters or missing_locations:
        st.caption("待补齐：" + "、".join(missing_characters + missing_locations))

    _render_autonomous_reference_status(project, service)

    _render_source_promotions(project, story_revision, service)
    tabs = st.tabs(["资产总览", "角色详情", "场景详情", "候选对比 / 锁定"])
    selected_character_key = (
        f"reference-selected-{project.id}-{ReferenceBindingType.CHARACTER.value}"
    )
    selected_location_key = (
        f"reference-selected-{project.id}-{ReferenceBindingType.LOCATION.value}"
    )
    with tabs[0]:
        st.markdown("### 角色")
        _render_overview_cards(
            service,
            project,
            story.characters,
            ReferenceBindingType.CHARACTER,
            story_revision["id"],
        )
        st.markdown("### 场景")
        _render_overview_cards(
            service,
            project,
            story.locations,
            ReferenceBindingType.LOCATION,
            story_revision["id"],
        )
    with tabs[1]:
        if story.characters:
            labels = {subject.id: subject.name for subject in story.characters}
            selected_id = st.selectbox(
                "选择角色",
                list(labels),
                format_func=lambda value: labels[value],
                key=f"assets-character-detail-{project.id}",
            )
            st.session_state[selected_character_key] = selected_id
            subject = next(item for item in story.characters if item.id == selected_id)
            _render_workspace(
                service,
                storage,
                project,
                subject,
                ReferenceBindingType.CHARACTER,
                story_revision["id"],
                key_suffix="-character-detail",
            )
        else:
            st.info("当前故事还没有角色。")
    with tabs[2]:
        if story.locations:
            labels = {subject.id: subject.name for subject in story.locations}
            selected_id = st.selectbox(
                "选择场景",
                list(labels),
                format_func=lambda value: labels[value],
                key=f"assets-location-detail-{project.id}",
            )
            st.session_state[selected_location_key] = selected_id
            subject = next(item for item in story.locations if item.id == selected_id)
            _render_workspace(
                service,
                storage,
                project,
                subject,
                ReferenceBindingType.LOCATION,
                story_revision["id"],
                key_suffix="-scene-detail",
            )
        else:
            st.info("当前故事还没有场景。")
    with tabs[3]:
        kind = st.radio(
            "候选类型",
            ["角色", "场景"],
            horizontal=True,
            key=f"assets-candidate-kind-{project.id}",
        )
        subjects = story.characters if kind == "角色" else story.locations
        binding_type = (
            ReferenceBindingType.CHARACTER
            if kind == "角色"
            else ReferenceBindingType.LOCATION
        )
        if subjects:
            labels = {subject.id: subject.name for subject in subjects}
            selected_id = st.selectbox(
                "选择对象",
                list(labels),
                format_func=lambda value: labels[value],
                key=f"assets-candidate-subject-{project.id}",
            )
            subject = next(item for item in subjects if item.id == selected_id)
            _render_workspace(
                service,
                storage,
                project,
                subject,
                binding_type,
                story_revision["id"],
                key_suffix="-candidate-compare",
            )
        else:
            st.info("当前故事还没有可比较的对象。")
