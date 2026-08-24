from __future__ import annotations

import streamlit as st

from aidrama_studio.components.page_header import page_header
from aidrama_studio.domain import ReferenceAssetType, ReferenceBindingType
from aidrama_studio.pages._shared import current_project_or_stop
from aidrama_studio.services import (
    ReferenceAssetService,
    ReferenceAssetServiceError,
    ReferenceAssetStorageError,
    ReferenceAssetStorageService,
)


def _brief(subject, binding_type: ReferenceBindingType) -> str:
    if binding_type is ReferenceBindingType.CHARACTER:
        return " · ".join(value for value in (subject.identity, subject.appearance, subject.personality) if value) or "暂无视觉摘要"
    return " · ".join(value for value in (subject.environment, subject.visual_style, subject.time_of_day) if value) or "暂无视觉摘要"


def _outdated(version, story_revision_id: str) -> bool:
    return bool(version and version.metadata.get("source_story_revision_id") != story_revision_id)


def _version_status(asset, version, story_revision_id: str) -> str:
    if version is None:
        return "NO REFERENCE"
    if _outdated(version, story_revision_id):
        return "REFERENCE OUTDATED"
    if asset.current_version_id == version.id:
        return "LOCKED"
    return "DRAFT"


def _asset_status(service, project, binding_type, subject_id, story_revision_id):
    asset = service.find_asset_for_binding(project.id, binding_type, subject_id)
    current = service.get_current_version(project.id, asset.id) if asset else None
    return asset, current, _version_status(asset, current, story_revision_id)


def _readiness(service, project, subjects, binding_type, story_revision_id):
    locked = []
    missing = []
    for subject in subjects:
        asset, current, status = _asset_status(service, project, binding_type, subject.id, story_revision_id)
        if status == "LOCKED":
            locked.append(subject.id)
        else:
            missing.append(subject.name)
    return len(locked), missing


def _render_card(service, project, subject, binding_type, story_revision_id):
    asset, current, status = _asset_status(service, project, binding_type, subject.id, story_revision_id)
    with st.container(border=True):
        st.markdown(f"### {subject.name}")
        st.caption(_brief(subject, binding_type))
        if status == "LOCKED":
            st.success(f"LOCKED · v{current.version_number}")
        elif status == "REFERENCE OUTDATED":
            st.warning("REFERENCE OUTDATED")
        elif asset:
            st.info("DRAFT · 尚未锁定")
        else:
            st.caption("NO REFERENCE")
        if current is not None:
            st.caption(f"Current reference · v{current.version_number} · {current.filename}")
            try:
                image_path = service.resolve_version_path(project.id, current.id)
            except ReferenceAssetServiceError:
                image_path = None
            if image_path is not None and image_path.exists():
                st.image(str(image_path), width=180)
        st.caption(f"{binding_type.value.title()} ID · `{subject.id}`")


def _ensure_asset(service, project, binding_type, subject_id):
    asset = service.find_asset_for_binding(project.id, binding_type, subject_id)
    if asset is None:
        asset_type = ReferenceAssetType.CHARACTER_REFERENCE if binding_type is ReferenceBindingType.CHARACTER else ReferenceAssetType.LOCATION_REFERENCE
        asset = service.create_asset(project.id, asset_type)
    return asset


def _import_uploads(service, storage, project, subject, binding_type, story_revision_id, uploads) -> int:
    """Import selected files as draft versions and bind each to its Story Bible subject."""
    asset = _ensure_asset(service, project, binding_type, subject.id)
    imported = 0
    for upload in uploads:
        try:
            version = storage.import_image(
                project.id,
                asset.id,
                upload.getvalue(),
                filename=upload.name,
                mime_type=upload.type or "",
                metadata={"source_story_revision_id": story_revision_id, "subject_id": subject.id},
            )
            service.bind_version(project.id, version.id, binding_type, subject.id)
            imported += 1
        except (ReferenceAssetStorageError, ReferenceAssetServiceError, ValueError) as exc:
            st.warning(f"{upload.name}：{exc}")
    return imported


def _render_workspace(service, storage, project, subject, binding_type, story_revision_id):
    asset, current, status = _asset_status(service, project, binding_type, subject.id, story_revision_id)
    st.markdown(f"## {subject.name} · Reference Workspace")
    st.caption(_brief(subject, binding_type))
    if status == "REFERENCE OUTDATED":
        st.warning("REFERENCE OUTDATED · 当前 Story Bible revision 已变化；不会自动替换。")
    elif status == "LOCKED":
        st.success(f"REFERENCE LOCKED · v{current.version_number}")

    st.markdown("### Candidate Reference Draft")
    st.caption("上传图片会创建新的不可变 Draft version；已锁定版本不会被覆盖。")
    uploads = st.file_uploader(
        "上传 JPEG / PNG / WebP",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        key=f"reference-upload-{project.id}-{binding_type.value}-{subject.id}",
    )
    if uploads and st.button("导入并创建 Draft version", type="primary", key=f"import-{project.id}-{binding_type.value}-{subject.id}"):
        imported = _import_uploads(service, storage, project, subject, binding_type, story_revision_id, uploads)
        if imported:
            st.success(f"已创建 {imported} 个 Draft version")
            st.rerun()

    if not asset:
        st.info("暂无 Reference Asset。上传图片后会自动创建。")
        return
    versions = service.list_versions(project.id, asset.id)
    st.markdown("### Version history")
    for version in reversed(versions):
        version_status = _version_status(asset, version, story_revision_id)
        with st.expander(f"v{version.version_number} · {version_status} · {version.filename}", expanded=version.id == asset.current_version_id):
            st.caption(f"{version.mime_type} · {version.size_bytes} bytes · SHA256 {version.sha256[:12]}…")
            image_path = service.resolve_version_path(project.id, version.id)
            if image_path.exists():
                st.image(str(image_path), width=260)
            else:
                st.error("Reference image 文件不可用。")
            if version.id != asset.current_version_id:
                if st.button("Lock version", type="primary", key=f"lock-{version.id}"):
                    service.activate_version(project.id, asset.id, version.id)
                    st.success("REFERENCE LOCKED")
                    st.rerun()
            else:
                st.success("LOCKED version · 不可覆盖或删除")


def _render_subject_tab(service, storage, project, subjects, binding_type, story_revision_id):
    if not subjects:
        st.info("当前 Story Bible 没有可用 subject。")
        return
    key = f"reference-selected-{project.id}-{binding_type.value}"
    selected_id = st.session_state.get(key, subjects[0].id)
    cards = st.columns(min(3, len(subjects)))
    for index, subject in enumerate(subjects):
        with cards[index % len(cards)]:
            _render_card(service, project, subject, binding_type, story_revision_id)
            if st.button("打开 Workspace", key=f"open-{project.id}-{binding_type.value}-{subject.id}", use_container_width=True):
                st.session_state[key] = subject.id
                st.rerun()
    selected = next((subject for subject in subjects if subject.id == selected_id), subjects[0])
    st.divider()
    _render_workspace(service, storage, project, selected, binding_type, story_revision_id)


def render() -> None:
    page_header("角色与场景", "REFERENCE ASSET CENTER", "管理角色、场景与可锁定的视觉参考资产。")
    project = current_project_or_stop()
    service = ReferenceAssetService()
    storage = ReferenceAssetStorageService(service)
    story_revision = service.approved_story_revision(project.id)
    if story_revision is None:
        st.warning("请先确认 Story Bible。")
        return
    story = story_revision["content"]
    characters_locked, missing_characters = _readiness(service, project, story.characters, ReferenceBindingType.CHARACTER, story_revision["id"])
    locations_locked, missing_locations = _readiness(service, project, story.locations, ReferenceBindingType.LOCATION, story_revision["id"])
    st.markdown("## Reference Readiness")
    metrics = st.columns(5)
    metrics[0].metric("Characters", len(story.characters))
    metrics[1].metric("Character locked", characters_locked)
    metrics[2].metric("Locations", len(story.locations))
    metrics[3].metric("Location locked", locations_locked)
    metrics[4].metric("Missing coverage", len(missing_characters) + len(missing_locations))
    if missing_characters or missing_locations:
        st.caption("Missing: " + ", ".join(missing_characters + missing_locations))
    tabs = st.tabs(["Characters", "Locations", "Styles", "Props"])
    with tabs[0]:
        _render_subject_tab(service, storage, project, story.characters, ReferenceBindingType.CHARACTER, story_revision["id"])
    with tabs[1]:
        _render_subject_tab(service, storage, project, story.locations, ReferenceBindingType.LOCATION, story_revision["id"])
    with tabs[2]:
        st.info("Styles reference assets will be enabled in a later scope.")
    with tabs[3]:
        st.info("Props reference assets will be enabled in a later scope.")
