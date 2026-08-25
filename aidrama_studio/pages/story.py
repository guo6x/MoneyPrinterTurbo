from __future__ import annotations

from copy import deepcopy

import streamlit as st
from loguru import logger

from aidrama_studio.components.page_header import page_header
from aidrama_studio.domain import (
    Character, Location, StoryBible, StoryRevisionStatus, ScriptRevisionStatus,
    ScriptBeatType, InteriorExterior, TimeOfDay, Scene, ScriptBeat, StructuredScript,
)
from aidrama_studio.pages._shared import current_project_or_stop
from aidrama_studio.services import (
    CreativeIntakeService,
    ScriptService,
    ScriptServiceError,
    StoryService,
    StoryServiceError,
)


BEAT_TYPES = ["OPENING", "DEVELOPMENT", "TURNING_POINT", "CLIMAX", "ENDING"]


def _go_settings() -> None:
    from aidrama_studio.components.navigation import request_navigation

    request_navigation("settings")


def _working_key(revision_id: str) -> str:
    return f"story_working_{revision_id}"


def _revision_label(revision: dict) -> str:
    return f"v{revision['version']} · {revision['status'].value}"


def _get_working(revision: dict) -> dict:
    key = _working_key(revision["id"])
    if key not in st.session_state:
        st.session_state[key] = revision["content"].model_dump(mode="python")
    return st.session_state[key]


def _input(label: str, value: str, key: str, *, area: bool = False, **kwargs) -> str:
    if area:
        return st.text_area(label, value=value, key=key, **kwargs)
    return st.text_input(label, value=value, key=key, **kwargs)


def _list_text(values: list[str], separator: str = ", ") -> str:
    return separator.join(values)


def _parse_list(value: str, separator: str = ",") -> list[str]:
    return [item.strip() for item in value.replace("，", separator).split(separator) if item.strip()]


def _render_creative_intake(project) -> None:
    service = CreativeIntakeService()
    source_pack = service.source_pack
    st.markdown("### Creative Intake / Source Pack")
    st.caption(
        "一句创意、现有文档和多张参考图会先进入项目隔离的 Source Pack；"
        "原文件与 SHA-256 保持不变，不会自动发送到云端。"
    )

    idea_key = f"intake-idea-{project.id}"
    idea = st.text_area(
        "一句创意或长 Brief",
        key=idea_key,
        height=120,
        placeholder="例如：失忆的末班车司机，在终点站遇见未来的自己。",
    )
    if st.button(
        "加入 Source Pack",
        key=f"intake-add-text-{project.id}",
        disabled=not idea.strip(),
    ):
        try:
            source_pack.import_text(project.id, idea, filename="creative-idea.txt")
            st.toast("创意文本已安全加入 Source Pack")
            st.rerun()
        except Exception as exc:
            st.error(f"创意文本导入失败：{exc}")

    uploads = st.file_uploader(
        "导入规划文档 / 剧本 / 分镜 / 参考图片",
        type=["txt", "md", "pdf", "docx", "pptx", "png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        key=f"intake-files-{project.id}",
        help="支持多选；扩展名、MIME、文件签名、压缩包结构和图片内容都会校验。",
    )
    if st.button(
        "导入所选文件",
        key=f"intake-import-files-{project.id}",
        disabled=not uploads,
    ):
        imported = 0
        try:
            for upload in uploads or []:
                source_pack.import_bytes(
                    project.id,
                    upload.name,
                    upload.getvalue(),
                    mime_type=upload.type or None,
                )
                imported += 1
            st.toast(f"已处理 {imported} 个 Source Pack 文件")
            st.rerun()
        except Exception as exc:
            st.error(f"文件导入失败（已成功的条目保持不变）：{exc}")

    items = source_pack.list(project.id)
    if not items:
        st.info("Source Pack 还是空的。先输入一句创意，或选择现有素材。")
        return

    st.markdown("#### 当前 Source Pack")
    analyses = service.repository.list_intake_analyses(project.id)
    latest_analysis = {item.source_id: item for item in analyses}
    for item in items:
        analysis = latest_analysis.get(item.id)
        with st.container(border=True):
            name_col, action_col = st.columns([4, 1])
            name_col.markdown(f"**{item.display_filename}**")
            name_col.caption(
                f"{item.source_kind.value} · {item.extraction_state.value} · "
                f"{item.size_bytes} bytes · SHA256 {item.sha256[:12]}…"
            )
            if analysis is not None:
                name_col.caption("识别：" + " / ".join(analysis.classifications))
            if action_col.button(
                "分析",
                key=f"intake-analyze-{item.id}",
                use_container_width=True,
            ):
                try:
                    service.analyzer.analyze(project.id, item.id)
                    st.rerun()
                except Exception as exc:
                    st.error(f"素材分析失败：{exc}")
            if item.extracted_text:
                with st.expander("查看提取文本（仅本地）"):
                    st.text(item.extracted_text[:8000])

    labels = {item.id: item.display_filename for item in items}
    selected = st.multiselect(
        "用于规范化 Creative Brief 的来源",
        options=list(labels),
        default=list(labels),
        format_func=lambda source_id: labels[source_id],
        key=f"intake-normalize-sources-{project.id}",
    )
    genre = st.text_input("规范化类型（可选）", key=f"intake-genre-{project.id}")
    tone = st.text_input("规范化基调（可选）", key=f"intake-tone-{project.id}")
    if st.button(
        "生成本地规范化 Brief",
        type="primary",
        key=f"intake-normalize-{project.id}",
        disabled=not selected,
    ):
        try:
            brief = service.normalize(
                project.id,
                source_ids=selected,
                overrides={"genre": genre.strip(), "tone": tone.strip()},
            )
            st.session_state[f"story-brief-{project.id}"] = brief.premise
            if brief.genre:
                st.session_state[f"story-genre-{project.id}"] = brief.genre
            if brief.tone:
                st.session_state[f"story-tone-{project.id}"] = brief.tone
            st.session_state[f"story-source-ids-{project.id}"] = tuple(brief.source_ids)
            st.session_state[f"story-normalized-brief-{project.id}"] = brief.id
            st.toast("规范化 Brief 已创建，并已带入 Story Bible 创作区")
            st.rerun()
        except Exception as exc:
            st.error(f"规范化失败：{exc}")

    briefs = service.repository.list_normalized_creative_briefs(project.id)
    if briefs:
        latest = briefs[-1]
        with st.expander("最新规范化 Brief", expanded=True):
            st.markdown(f"**{latest.title_candidate or '未命名 Brief'}**")
            st.write(latest.premise or "暂无可提取文本；图片仍保留在 Source Pack。")
            st.caption(f"来源 {len(latest.source_ids)} 项 · {latest.status}")

    approved_story = next(
        (
            revision
            for revision in service.repository.list_story_revisions(project.id)
            if revision["status"] is StoryRevisionStatus.APPROVED
        ),
        None,
    )
    image_items = [item for item in items if item.source_kind.value == "IMAGE"]
    if approved_story is not None and image_items:
        st.markdown("#### 提升为锁定参考图")
        st.caption("提升会保留 Source Pack 来源 ID 与 SHA-256，不会覆盖已有参考版本。")
        image_labels = {item.id: item.display_filename for item in image_items}
        source_id = st.selectbox(
            "Source Pack 图片",
            options=list(image_labels),
            format_func=lambda item_id: image_labels[item_id],
            key=f"intake-promote-source-{project.id}",
        )
        targets = {
            **{
                f"CHARACTER:{item.id}": f"角色 · {item.name}"
                for item in approved_story["content"].characters
            },
            **{
                f"LOCATION:{item.id}": f"场景 · {item.name}"
                for item in approved_story["content"].locations
            },
        }
        if targets:
            target = st.selectbox(
                "绑定目标",
                options=list(targets),
                format_func=lambda item_id: targets[item_id],
                key=f"intake-promote-target-{project.id}",
            )
            if st.button(
                "提升并锁定 Reference",
                key=f"intake-promote-{project.id}",
            ):
                binding_type, binding_id = target.split(":", 1)
                try:
                    service.promote_image_reference(
                        project.id,
                        source_id,
                        source_story_revision_id=approved_story["id"],
                        binding_type=binding_type,
                        binding_id=binding_id,
                        lock=True,
                    )
                    st.toast("Reference 已创建、绑定并锁定")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Reference 提升失败：{exc}")


def _render_brief(project, service: StoryService) -> None:
    st.markdown("### 创作 Brief")
    st.caption("Brief 只保存创作输入，不会保存 API Key、Base URL 或其他凭据。")
    brief_key = f"story-brief-{project.id}"
    genre_key = f"story-genre-{project.id}"
    tone_key = f"story-tone-{project.id}"
    audience_key = f"story-audience-{project.id}"
    constraints_key = f"story-constraints-{project.id}"
    _input(
        "核心创意 / 项目 Brief",
        st.session_state.get(brief_key, project.description),
        brief_key,
        area=True,
        height=155,
        placeholder="例如：一个失去记忆的夜班司机，在最后一班车上遇见未来的自己。",
    )
    _input("类型", st.session_state.get(genre_key, "都市悬疑"), genre_key)
    _input("基调", st.session_state.get(tone_key, "克制、紧张"), tone_key)
    _input(
        "目标观众（可选）",
        st.session_state.get(audience_key, ""),
        audience_key,
        placeholder="例如：喜欢反转悬疑的短视频观众",
    )
    _input(
        "创作约束（可选）",
        st.session_state.get(constraints_key, ""),
        constraints_key,
        area=True,
        height=95,
        placeholder="例如：不出现大型群像；结尾保留一个视觉钩子。",
    )

    ready, detail = service.llm_readiness(project.id)
    if not ready:
        st.info(f"AI 服务尚未配置：{detail}")
        if st.button("前往设置", key=f"story-settings-{project.id}"):
            _go_settings()
    if st.button(
        "AI 生成 Story Bible",
        type="primary",
        use_container_width=True,
        disabled=not ready,
        key=f"generate-story-{project.id}",
    ):
        try:
            with st.spinner("正在构建人物与世界观…"):
                revision = service.generate_story_bible(
                    project,
                    brief=st.session_state.get(brief_key, ""),
                    genre=st.session_state.get(genre_key, ""),
                    tone=st.session_state.get(tone_key, ""),
                    target_audience=st.session_state.get(audience_key, ""),
                    creative_constraints=st.session_state.get(constraints_key, ""),
                    source_ids=tuple(
                        st.session_state.get(f"story-source-ids-{project.id}", ())
                    ),
                    normalized_brief_id=st.session_state.get(
                        f"story-normalized-brief-{project.id}"
                    ),
                )
            st.session_state.story_revision_id = revision["id"]
            st.toast(f"Story Bible v{revision['version']} 已生成")
            st.rerun()
        except StoryServiceError as exc:
            st.error(str(exc))
        except Exception:
            logger.exception("story page generation failed")
            st.error("Story Bible 生成失败，请稍后重试。")

    if st.button("创建空白 Story Bible", use_container_width=True, key=f"blank-story-{project.id}"):
        try:
            revision = service.create_blank_draft(project)
            st.session_state.story_revision_id = revision["id"]
            st.toast("已创建可编辑的 Story Bible Draft")
            st.rerun()
        except Exception:
            logger.exception("failed to create blank Story Bible")
            st.error("空白 Story Bible 创建失败，请稍后重试。")

    st.markdown("### 版本历史")
    revisions = service.list_revisions(project.id)
    if not revisions:
        st.caption("还没有 revision。你可以先生成或创建一个空白 Draft。")
    for revision in revisions:
        with st.container(border=True):
            select_col, action_col = st.columns([3, 1])
            select_col.markdown(f"**{_revision_label(revision)}**")
            select_col.caption(revision["updated_at"].replace("T", " ").split(".")[0])
            if action_col.button("查看", key=f"view-revision-{revision['id']}", use_container_width=True):
                st.session_state.story_revision_id = revision["id"]
                st.rerun()
            if revision["status"] is StoryRevisionStatus.APPROVED:
                if action_col.button("新 Draft", key=f"fork-revision-{revision['id']}", use_container_width=True):
                    try:
                        forked = service.create_revision_from_approved(revision["id"])
                        st.session_state.story_revision_id = forked["id"]
                        st.rerun()
                    except Exception:
                        logger.exception("failed to fork approved Story Bible")
                        st.error("无法从该版本创建 Draft。")


def _render_overview(working: dict, revision_id: str) -> None:
    st.markdown("#### 故事概览")
    working["title"] = _input("标题", working["title"], f"{revision_id}-title")
    working["logline"] = _input("Logline", working["logline"], f"{revision_id}-logline", area=True, height=90)
    working["premise"] = _input("故事前提", working["premise"], f"{revision_id}-premise", area=True, height=115)
    left, right = st.columns(2)
    with left:
        working["genre"] = _input("类型", working["genre"], f"{revision_id}-genre")
        working["tone"] = _input("基调", working["tone"], f"{revision_id}-tone")
    with right:
        themes = _input("主题（逗号分隔）", _list_text(working["themes"]), f"{revision_id}-themes")
        working["themes"] = _parse_list(themes)
    st.markdown("#### 世界观")
    world = working["world"]
    left, right = st.columns(2)
    with left:
        world["era"] = _input("时代", world["era"], f"{revision_id}-world-era")
        world["setting"] = _input("世界设定", world["setting"], f"{revision_id}-world-setting", area=True, height=100)
    with right:
        rules = _input("世界规则（每行一条）", "\n".join(world["rules"]), f"{revision_id}-world-rules", area=True, height=100)
        world["rules"] = [line.strip() for line in rules.splitlines() if line.strip()]
        world["timeline_notes"] = _input("时间线备注", world["timeline_notes"], f"{revision_id}-world-timeline", area=True, height=80)


def _render_characters(working: dict, revision_id: str) -> None:
    st.markdown("#### 人物")
    st.caption("人物使用稳定内部 ID；被 Story Beat 引用的人物不能直接删除。")
    remove_id = None
    for character in working["characters"]:
        with st.container(border=True):
            top_left, top_right = st.columns([4, 1])
            top_left.markdown(f"**{character['id']} · {character['name']}**")
            if top_right.button("移除", key=f"remove-char-{revision_id}-{character['id']}"):
                remove_id = character["id"]
            character["name"] = _input("姓名", character["name"], f"{revision_id}-{character['id']}-name")
            character["role"] = _input("角色定位", character["role"], f"{revision_id}-{character['id']}-role")
            character["identity"] = _input("身份", character["identity"], f"{revision_id}-{character['id']}-identity", area=True, height=70)
            character["personality"] = _input("性格", character["personality"], f"{revision_id}-{character['id']}-personality", area=True, height=70)
            character["appearance"] = _input("外观", character["appearance"], f"{revision_id}-{character['id']}-appearance", area=True, height=70)
            character["motivation"] = _input("动机", character["motivation"], f"{revision_id}-{character['id']}-motivation", area=True, height=70)
            character["relationship_notes"] = _input("关系备注", character["relationship_notes"], f"{revision_id}-{character['id']}-relationships", area=True, height=70)
            character["speech_style"] = _input("说话方式", character["speech_style"], f"{revision_id}-{character['id']}-speech")
            character["age_or_range"] = _input("年龄段", character["age_or_range"], f"{revision_id}-{character['id']}-age")
    if remove_id:
        referenced = any(remove_id in beat["characters"] for beat in working["story_beats"])
        if referenced:
            st.error("该人物仍被 Story Beat 引用，请先解除引用后再移除。")
        elif len(working["characters"]) <= 1:
            st.error("Story Bible 至少需要保留一个人物。")
        else:
            working["characters"] = [item for item in working["characters"] if item["id"] != remove_id]
            st.rerun()
    if st.button("+ 添加人物", key=f"add-char-{revision_id}"):
        index = len(working["characters"]) + 1
        working["characters"].append(Character(id=f"char_{index:03d}", name=f"新人物 {index}").model_dump())
        st.rerun()


def _render_locations(working: dict, revision_id: str) -> None:
    st.markdown("#### 场景")
    st.caption("被 Story Beat 引用的场景不能直接删除，避免产生悬空引用。")
    remove_id = None
    for location in working["locations"]:
        with st.container(border=True):
            top_left, top_right = st.columns([4, 1])
            top_left.markdown(f"**{location['id']} · {location['name']}**")
            if top_right.button("移除", key=f"remove-loc-{revision_id}-{location['id']}"):
                remove_id = location["id"]
            location["name"] = _input("名称", location["name"], f"{revision_id}-{location['id']}-name")
            location["function"] = _input("叙事功能", location["function"], f"{revision_id}-{location['id']}-function", area=True, height=70)
            left, right = st.columns(2)
            with left:
                location["environment"] = _input("环境", location["environment"], f"{revision_id}-{location['id']}-environment", area=True, height=80)
                location["visual_style"] = _input("视觉风格", location["visual_style"], f"{revision_id}-{location['id']}-visual", area=True, height=80)
            with right:
                location["time_of_day"] = _input("时间", location["time_of_day"], f"{revision_id}-{location['id']}-time")
                props = _input("关键道具（逗号分隔）", _list_text(location["key_props"]), f"{revision_id}-{location['id']}-props")
                location["key_props"] = _parse_list(props)
    if remove_id:
        referenced = any(remove_id == beat["location_id"] for beat in working["story_beats"])
        if referenced:
            st.error("该场景仍被 Story Beat 引用，请先解除引用后再移除。")
        elif len(working["locations"]) <= 1:
            st.error("Story Bible 至少需要保留一个场景。")
        else:
            working["locations"] = [item for item in working["locations"] if item["id"] != remove_id]
            st.rerun()
    if st.button("+ 添加场景", key=f"add-loc-{revision_id}"):
        index = len(working["locations"]) + 1
        working["locations"].append(Location(id=f"loc_{index:03d}", name=f"新场景 {index}").model_dump())
        st.rerun()


def _render_beats(working: dict, revision_id: str) -> None:
    st.markdown("#### 剧情结构")
    st.caption("order 可直接编辑；保存时会检查顺序、人物引用和场景引用。")
    character_options = [item["id"] for item in working["characters"]]
    location_options = [item["id"] for item in working["locations"]]
    for beat in sorted(working["story_beats"], key=lambda item: item["order"]):
        with st.container(border=True):
            st.markdown(f"**{beat['id']} · Beat {beat['order']}**")
            left, middle, right = st.columns([1, 2, 2])
            with left:
                beat["order"] = st.number_input("顺序", min_value=1, max_value=100, value=int(beat["order"]), key=f"{revision_id}-{beat['id']}-order")
            with middle:
                beat["type"] = st.selectbox("类型", BEAT_TYPES, index=BEAT_TYPES.index(beat["type"]) if beat["type"] in BEAT_TYPES else 0, key=f"{revision_id}-{beat['id']}-type")
            with right:
                selected_location = st.selectbox("场景", ["（无）"] + location_options, index=(location_options.index(beat["location_id"]) + 1) if beat["location_id"] in location_options else 0, key=f"{revision_id}-{beat['id']}-location")
                beat["location_id"] = None if selected_location == "（无）" else selected_location
            beat["summary"] = _input("摘要", beat["summary"], f"{revision_id}-{beat['id']}-summary", area=True, height=80)
            beat["characters"] = st.multiselect("出场人物", character_options, default=[item for item in beat["characters"] if item in character_options], key=f"{revision_id}-{beat['id']}-characters")
            beat["emotional_goal"] = _input("情绪目标", beat["emotional_goal"], f"{revision_id}-{beat['id']}-emotion")


def _build_content(working: dict) -> StoryBible:
    return StoryBible.model_validate(deepcopy(working))


def _render_bible_editor(project, service: StoryService, revision: dict) -> None:
    working = _get_working(revision)
    revision_id = revision["id"]
    status = revision["status"]
    status_text = {StoryRevisionStatus.DRAFT: "DRAFT", StoryRevisionStatus.APPROVED: "APPROVED · 已确认", StoryRevisionStatus.SUPERSEDED: "SUPERSEDED"}[status]
    st.markdown(f'<div class="story-revision-bar"><strong>Story Bible v{revision["version"]}</strong><span class="story-revision-status">{status_text}</span></div>', unsafe_allow_html=True)
    overview, characters, locations, beats = st.tabs(["故事概览", "人物", "场景", "剧情结构"])
    with overview:
        _render_overview(working, revision_id)
    with characters:
        _render_characters(working, revision_id)
    with locations:
        _render_locations(working, revision_id)
    with beats:
        _render_beats(working, revision_id)

    save_label = "保存修改" if status is StoryRevisionStatus.DRAFT else "保存为新 Draft"
    save_col, approve_col = st.columns([2, 1])
    if save_col.button(save_label, type="primary", use_container_width=True, key=f"save-story-{revision_id}"):
        try:
            saved = service.save_draft(project.id, _build_content(working), revision_id=revision_id)
            st.session_state.story_revision_id = saved["id"]
            st.toast(f"Story Bible v{saved['version']} 已保存")
            st.rerun()
        except ValueError as exc:
            st.error(f"无法保存：{exc}")
        except Exception:
            logger.exception("failed to save Story Bible draft")
            st.error("保存失败，请检查必填字段和引用关系。")
    if status is StoryRevisionStatus.DRAFT:
        if approve_col.button("确认 Story Bible", use_container_width=True, key=f"approve-story-{revision_id}"):
            try:
                saved = service.save_draft(project.id, _build_content(working), revision_id=revision_id)
                approved = service.approve_revision(saved["id"])
                st.session_state.story_revision_id = approved["id"]
                st.success("Story Bible 已确认。结构化剧本将在下一阶段生成。")
                st.rerun()
            except ValueError as exc:
                st.error(f"确认前校验失败：{exc}")
            except Exception:
                logger.exception("failed to approve Story Bible")
                st.error("确认失败，请稍后重试。")
    elif status is StoryRevisionStatus.APPROVED:
        approve_col.success("已确认")


def _script_working_key(revision_id: str) -> str:
    return f"script_working_{revision_id}"


def _render_script_editor(project, story_revision: dict, script_service: ScriptService) -> None:
    ready, detail = script_service.llm_readiness(project.id)
    if ready and st.button("AI 生成 Structured Script", key=f"generate-script-{project.id}"):
        try:
            generated = script_service.generate_script(project)
            st.session_state.script_revision_id = generated["id"]; st.toast("Structured Script 已生成"); st.rerun()
        except ScriptServiceError as exc:
            st.error(str(exc))
    elif not ready:
        st.caption(f"AI 生成不可用：{detail}；仍可手动创建剧本。")
    revisions = script_service.list_revisions(project.id)
    if not revisions:
        st.info("还没有 Structured Script。请先确认 Story Bible，再手动创建或生成剧本。")
        if story_revision["status"] is StoryRevisionStatus.APPROVED:
            if st.button("创建空白 Structured Script", type="primary", key=f"create-script-{project.id}"):
                try:
                    script = script_service.create_manual_script(project, story_revision)
                    st.session_state.script_revision_id = script["id"]
                    st.rerun()
                except Exception as exc:
                    st.error(f"创建失败：{exc}")
        return
    current_id = st.session_state.get("script_revision_id")
    revision = script_service.get_revision(current_id) if current_id else revisions[0]
    if revision is None:
        revision = revisions[0]
    st.session_state.script_revision_id = revision["id"]
    history = st.selectbox("Structured Script 历史", revisions, index=next((i for i, x in enumerate(revisions) if x["id"] == revision["id"]), 0), format_func=lambda x: f"v{x['version']} · {x['status'].value}", key=f"script-history-{project.id}")
    if history["id"] != revision["id"]:
        st.session_state.script_revision_id = history["id"]; st.rerun()
    if revision["status"] is ScriptRevisionStatus.APPROVED and st.button("从此版本创建新 Draft", key=f"fork-script-{revision['id']}"):
        try:
            forked = script_service.create_revision_from_approved(revision["id"]); st.session_state.script_revision_id = forked["id"]; st.rerun()
        except Exception as exc: st.error(f"无法创建 Draft：{exc}")
    status = revision["status"]
    if script_service.is_outdated(revision):
        st.warning("当前 Structured Script 基于旧版 Story Bible（outdated），请从最新 APPROVED Story Bible 创建新 Draft 后再确认。")
    status_text = {ScriptRevisionStatus.DRAFT: "DRAFT", ScriptRevisionStatus.APPROVED: "APPROVED · 已确认", ScriptRevisionStatus.SUPERSEDED: "SUPERSEDED"}[status]
    st.markdown(f'<div class="story-revision-bar"><strong>Structured Script v{revision["version"]}</strong><span class="story-revision-status">{status_text}</span></div>', unsafe_allow_html=True)
    working_key = _script_working_key(revision["id"])
    if working_key not in st.session_state:
        st.session_state[working_key] = revision["content"].model_dump(mode="python")
    working = st.session_state[working_key]
    working["title"] = _input("剧本标题", working["title"], f"{revision['id']}-script-title")
    working["summary"] = _input("剧本摘要", working.get("summary", ""), f"{revision['id']}-script-summary", area=True, height=80)
    character_options = [c["id"] for c in story_revision["content"].model_dump(mode="python")["characters"]]
    location_options = [l["id"] for l in story_revision["content"].model_dump(mode="python")["locations"]]
    st.markdown("#### Scene Navigator")
    scene_options = sorted(working["scenes"], key=lambda x: x["order"])
    selected_scene = st.selectbox(
        "当前 Scene",
        scene_options,
        format_func=lambda x: f"Scene {int(x['order']):02d} · {x['title']} · {x['location_id']} · {x.get('estimated_duration_seconds', 0):g}s",
        key=f"scene-navigator-{revision['id']}",
    )
    st.caption("Scene Navigator 保持当前选择；下方编辑器展示全部场景，便于快速浏览与编辑。")
    for scene in sorted(working["scenes"], key=lambda x: x["order"]):
        with st.container(border=True):
            st.markdown(f"**{scene['id']} · Scene {scene['order']}**")
            scene_move_left, scene_move_right = st.columns([1, 1])
            if scene_move_left.button("Scene ↑", key=f"scene-up-{revision['id']}-{scene['id']}"):
                previous = next((x for x in working["scenes"] if x["order"] == scene["order"] - 1), None)
                if previous:
                    previous["order"], scene["order"] = scene["order"], previous["order"]
                    st.rerun()
            if scene_move_right.button("Scene ↓", key=f"scene-down-{revision['id']}-{scene['id']}"):
                following = next((x for x in working["scenes"] if x["order"] == scene["order"] + 1), None)
                if following:
                    following["order"], scene["order"] = scene["order"], following["order"]
                    st.rerun()
            c1, c2, c3 = st.columns(3)
            scene["order"] = c1.number_input("顺序", 1, 999, int(scene["order"]), key=f"{revision['id']}-{scene['id']}-order")
            scene["title"] = c2.text_input("场景标题", scene["title"], key=f"{revision['id']}-{scene['id']}-title")
            scene["location_id"] = c3.selectbox("地点", location_options, index=max(0, location_options.index(scene["location_id"]) if scene["location_id"] in location_options else 0), key=f"{revision['id']}-{scene['id']}-loc")
            ie_value = getattr(scene.get("interior_exterior"), "value", scene.get("interior_exterior", "INT"))
            tod_value = getattr(scene.get("time_of_day"), "value", scene.get("time_of_day", "UNSPECIFIED"))
            scene["interior_exterior"] = st.selectbox("内外景", [x.value for x in InteriorExterior], index=[x.value for x in InteriorExterior].index(ie_value), key=f"{revision['id']}-{scene['id']}-ie")
            scene["time_of_day"] = st.selectbox("时间", [x.value for x in TimeOfDay], index=[x.value for x in TimeOfDay].index(tod_value), key=f"{revision['id']}-{scene['id']}-tod")
            scene["character_ids"] = st.multiselect("出场人物", character_options, default=[x for x in scene.get("character_ids", []) if x in character_options], key=f"{revision['id']}-{scene['id']}-chars")
            scene["purpose"] = _input("叙事目的", scene.get("purpose", ""), f"{revision['id']}-{scene['id']}-purpose")
            scene["summary"] = _input("场景摘要", scene.get("summary", ""), f"{revision['id']}-{scene['id']}-summary", area=True, height=70)
            scene["emotion"] = _input("情绪", scene.get("emotion", ""), f"{revision['id']}-{scene['id']}-emotion")
            scene["estimated_duration_seconds"] = st.number_input("预计时长（秒）", min_value=0.1, value=float(scene.get("estimated_duration_seconds", 1)), key=f"{revision['id']}-{scene['id']}-duration")
            for beat in sorted(scene["beats"], key=lambda x: x["order"]):
                b1, b2, b3 = st.columns([1, 2, 3])
                move_up, move_down = st.columns([1, 1])
                if move_up.button("Beat ↑", key=f"beat-up-{revision['id']}-{scene['id']}-{beat['id']}"):
                    previous = next((x for x in scene["beats"] if x["order"] == beat["order"] - 1), None)
                    if previous:
                        previous["order"], beat["order"] = beat["order"], previous["order"]
                        st.rerun()
                if move_down.button("Beat ↓", key=f"beat-down-{revision['id']}-{scene['id']}-{beat['id']}"):
                    following = next((x for x in scene["beats"] if x["order"] == beat["order"] + 1), None)
                    if following:
                        following["order"], beat["order"] = beat["order"], following["order"]
                        st.rerun()
                beat["order"] = b1.number_input("Beat 顺序", 1, 999, int(beat["order"]), key=f"{revision['id']}-{beat['id']}-order")
                beat_type = getattr(beat.get("type"), "value", beat.get("type", "ACTION"))
                beat["type"] = b2.selectbox("类型", [x.value for x in ScriptBeatType], index=[x.value for x in ScriptBeatType].index(beat_type), key=f"{revision['id']}-{beat['id']}-type")
                beat["character_id"] = b3.selectbox("人物", ["（无）"] + character_options, index=(character_options.index(beat["character_id"])+1 if beat.get("character_id") in character_options else 0), key=f"{revision['id']}-{beat['id']}-char")
                beat["character_id"] = None if beat["character_id"] == "（无）" else beat["character_id"]
                beat["text"] = _input("Beat 文本", beat.get("text", ""), f"{revision['id']}-{beat['id']}-text", area=True, height=70)
                beat["emotion"] = _input("Beat 情绪", beat.get("emotion") or "", f"{revision['id']}-{beat['id']}-emotion") or None
                beat["stage_direction"] = _input("舞台指示", beat.get("stage_direction") or "", f"{revision['id']}-{beat['id']}-direction") or None
            if st.button("+ 添加 Beat", key=f"add-beat-{revision['id']}-{scene['id']}"):
                idx = len(scene["beats"]) + 1
                scene["beats"].append(ScriptBeat(id=f"{scene['id']}_beat_{idx:03d}", order=idx, type=ScriptBeatType.ACTION, text="待填写").model_dump(mode="python")); st.rerun()
    if st.button("+ 添加 Scene", key=f"add-scene-{revision['id']}"):
        idx = len(working["scenes"]) + 1; loc = location_options[0]
        working["scenes"].append(Scene(id=f"scene_{idx:03d}", order=idx, title=f"Scene {idx}", location_id=loc, estimated_duration_seconds=1, beats=[ScriptBeat(id=f"beat_{idx:03d}_001", order=1, type=ScriptBeatType.ACTION, text="待填写")]).model_dump(mode="python")); st.rerun()
    save, approve = st.columns(2)
    if save.button("保存剧本", type="primary", key=f"save-script-{revision['id']}"):
        try:
            saved = script_service.save_draft(project.id, StructuredScript.model_validate(deepcopy(working)), revision_id=revision["id"])
            st.session_state.script_revision_id = saved["id"]; st.toast("Structured Script 已保存"); st.rerun()
        except Exception as exc: st.error(f"保存失败：{exc}")
    if status is ScriptRevisionStatus.DRAFT and approve.button("确认 Structured Script", key=f"approve-script-{revision['id']}"):
        try:
            saved = script_service.save_draft(project.id, StructuredScript.model_validate(deepcopy(working)), revision_id=revision["id"])
            script_service.approve_revision(saved["id"]); st.session_state.script_revision_id = saved["id"]; st.success("Structured Script 已确认"); st.rerun()
        except Exception as exc: st.error(f"确认失败：{exc}")
    with st.expander("阅读模式 / Script Preview"):
        st.caption("Preview 由结构化数据即时渲染，不是单独保存的 canonical 文本。")
        for scene in sorted(working["scenes"], key=lambda x: x["order"]):
            st.markdown(f"**SCENE {int(scene['order']):02d} — {scene['title']} — {scene.get('interior_exterior', 'INT')} — {scene.get('time_of_day', 'UNSPECIFIED')}**")
            for beat in sorted(scene["beats"], key=lambda x: x["order"]):
                label = beat.get("type", "ACTION")
                if hasattr(label, "value"): label = label.value
                st.markdown(f"**{label}**  {beat.get('text', '')}")


def render() -> None:
    page_header("创意与剧本", "STORY DEVELOPMENT", "从一句创意建立可生产的 Story Bible，并保留每一次 revision。")
    project = current_project_or_stop()
    intake_tab, development_tab = st.tabs(
        ["创意导入 / Source Pack", "Story Bible / Structured Script"]
    )
    with intake_tab:
        _render_creative_intake(project)
    with development_tab:
        service = StoryService()
        revisions = service.list_revisions(project.id)
        current_id = st.session_state.get("story_revision_id")
        revision = service.get_revision(current_id) if current_id else None
        if revision is None and revisions:
            revision = revisions[0]
            st.session_state.story_revision_id = revision["id"]
        left, right = st.columns([0.85, 1.65], gap="large")
        with left:
            _render_brief(project, service)
        with right:
            if revision is None:
                st.markdown("### Story Bible")
                st.info("右侧会展示结构化故事蓝图。你可以从左侧生成，或先创建空白 Draft。")
            else:
                script_service = ScriptService()
                bible_tab, script_tab = st.tabs(["Story Bible", "Structured Script"])
                with bible_tab:
                    _render_bible_editor(project, service, revision)
                with script_tab:
                    approved_story = next((item for item in service.list_revisions(project.id) if item["status"] is StoryRevisionStatus.APPROVED), None)
                    if approved_story is None:
                        st.warning("请先在 Story Bible 页确认一个 APPROVED 版本，才能编辑 Structured Script。")
                    else:
                        _render_script_editor(project, approved_story, script_service)
