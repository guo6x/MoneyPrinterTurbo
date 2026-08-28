"""Frontend contracts for the Forge MAX AI Production Workbench.

These tests intentionally sit above the durable AUTO service.  The page is
given an immutable :class:`AutoDecision` and a read-only fake service so a UI
regression cannot accidentally turn an AppTest run into a provider request.
The fake project uses a non-default duration to catch the common hard-coded
``60``-second hero regression.
"""

from __future__ import annotations

import json
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest

from aidrama_studio.domain import AutoAction, AutoDecision, AutoRunStatus, AutoStage


PROJECT_ID = "ui-workbench-project"
PROJECT_TITLE = "霓虹雨夜"
PROJECT_DURATION = 120
PROJECT_ASPECT = "9:16"


def _decision(
    *,
    status: AutoRunStatus,
    current_stage: AutoStage,
    next_action: AutoAction,
    why: str = "等待下一项正式工作。",
    completed: tuple[AutoStage, ...] = (),
    requires_human: bool = False,
    requires_paid: bool = False,
    requested_action: str | None = None,
    blocking_reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AutoDecision:
    """Build a valid decision while keeping all test state provider-neutral."""

    return AutoDecision(
        project_id=PROJECT_ID,
        status=status,
        current_stage=current_stage,
        next_action=next_action,
        why=why,
        blocking_reason=blocking_reason,
        requires_human=requires_human,
        requires_paid_authorization=requires_paid,
        requested_action=requested_action,
        resume_token="resume-token-for-ui" if requires_human else None,
        completed_stages=completed,
        input_state_hash="a" * 64,
        metadata=metadata or {},
    )


def _project() -> SimpleNamespace:
    return SimpleNamespace(
        id=PROJECT_ID,
        title=PROJECT_TITLE,
        target_duration_seconds=PROJECT_DURATION,
        aspect_ratio=PROJECT_ASPECT,
    )


def _capability_fixture() -> dict[str, dict[str, object]]:
    # The shared capability projection accepts these provider-neutral fields.
    # No provider name, endpoint, model id, or credential is put in the normal
    # page fixture.
    return {
        key: {
            "capability": key,
            "state": "READY",
            "configured": True,
            "verified": True,
            "runtime_available": True,
        }
        for key in ("text", "image", "video", "vision", "tts")
    }


def _app_for(
    decision: AutoDecision,
    *,
    paid_preview: dict[str, object] | None = None,
    events: list[dict[str, object]] | None = None,
    capabilities: dict[str, object] | None = None,
    keyframe_readiness: dict[str, object] | None = None,
):
    """Run ``auto.render`` with a strict, no-side-effect service double.

    The app script deliberately raises if the initial render tries to execute
    an AUTO transition.  This turns the ``REAL_*_CALLS=0`` requirement into a
    testable UI contract rather than a convention.
    """

    decision_payload = {
        "status": decision.status.value,
        "current_stage": decision.current_stage.value,
        "next_action": decision.next_action.value,
        "why": decision.why,
        "completed_stages": [item.value for item in decision.completed_stages],
        "requires_human": decision.requires_human,
        "requires_paid_authorization": decision.requires_paid_authorization,
        "requested_action": decision.requested_action,
        "blocking_reason": decision.blocking_reason,
        "resume_token": decision.resume_token,
        "metadata": decision.metadata,
    }
    preview_payload = paid_preview or {
        "required_create_count": 8,
        "per_item_max": 1,
        "retry_limit": 0,
        "provider_label": "已配置的视频方案",
        "authorization_fingerprint": "b" * 64,
    }
    events_payload = events or []
    capability_payload = capabilities or _capability_fixture()
    keyframe_payload = keyframe_readiness or {
        "gate": "PENDING",
        "planned_shot_count": 0,
        "validated_first_frame_count": 0,
        "missing_first_frame_count": 0,
        "invalid_first_frame_count": 0,
        "unintended_duplicate_first_frame_count": 0,
    }
    script = textwrap.dedent(
        f"""
        import streamlit as st
        from types import SimpleNamespace
        from aidrama_studio.domain import AutoAction, AutoDecision, AutoRunStatus, AutoStage
        from aidrama_studio.pages import auto

        project = SimpleNamespace(
            id={PROJECT_ID!r}, title={PROJECT_TITLE!r},
            target_duration_seconds={PROJECT_DURATION!r},
            aspect_ratio={PROJECT_ASPECT!r},
        )
        payload = {json.dumps(decision_payload, ensure_ascii=False)!r}
        raw = __import__('json').loads(payload)
        decision = AutoDecision(
            project_id={PROJECT_ID!r},
            status=AutoRunStatus(raw['status']),
            current_stage=AutoStage(raw['current_stage']),
            next_action=AutoAction(raw['next_action']),
            why=raw['why'],
            completed_stages=tuple(AutoStage(item) for item in raw['completed_stages']),
            requires_human=raw['requires_human'],
            requires_paid_authorization=raw['requires_paid_authorization'],
            requested_action=raw['requested_action'],
            blocking_reason=raw['blocking_reason'],
            resume_token=raw['resume_token'],
            input_state_hash='a' * 64,
            metadata=raw['metadata'],
        )

        class ReadOnlyService:
            def get_state(self, _project_id):
                return None

            def next_action(self, _project_id):
                return decision

            def list_events(self, _project_id):
                return __import__('json').loads({json.dumps(events_payload, ensure_ascii=False)!r})

            def keyframe_readiness(self, _project_id):
                return __import__('json').loads({json.dumps(keyframe_payload, ensure_ascii=False)!r})

            def preview_paid_authorization(self, _project_id):
                from aidrama_studio.domain import AutoPaidAuthorizationPreview
                preview = {json.dumps(preview_payload, ensure_ascii=False)!r}
                data = __import__('json').loads(preview)
                return AutoPaidAuthorizationPreview(
                    project_id={PROJECT_ID!r}, action=decision.next_action,
                    resource_key='production', input_state_hash='a' * 64,
                    authorization_fingerprint=data['authorization_fingerprint'],
                    required_create_count=data['required_create_count'],
                    per_item_max=data['per_item_max'], retry_limit=data['retry_limit'],
                    provider_label=data['provider_label'], details={{}},
                )

            def grant_paid_authorization(self, *_args, **_kwargs):
                raise AssertionError('UI must not grant paid authorization on initial render')

            def step(self, *_args, **_kwargs):
                raise AssertionError('UI render must not execute AUTO step/provider work')

            def resume(self, *_args, **_kwargs):
                raise AssertionError('UI render must not resume AUTO/provider work')

            def cancel(self, *_args, **_kwargs):
                raise AssertionError('UI render must not cancel on initial render')

        st.session_state['_aidrama_capability_snapshots'] = __import__('json').loads({json.dumps(capability_payload, ensure_ascii=False)!r})
        auto.current_project_or_stop = lambda: project
        auto.render_project_context = lambda *_args, **_kwargs: None
        auto.AutoOrchestratorService = lambda *_args, **_kwargs: ReadOnlyService()
        auto.render()
        """
    )
    from streamlit.testing.v1 import AppTest

    return AppTest.from_string(script, default_timeout=30).run()


def _all_visible_values(app) -> list[str]:
    """Collect normal text-like elements without traversing Advanced JSON."""

    values: list[str] = []
    for name in ("markdown", "caption", "text", "info", "warning", "success", "error", "title", "header", "subheader"):
        for item in getattr(app, name, ()):
            value = getattr(item, "value", None)
            if value is not None:
                values.append(str(value))
    for item in getattr(app, "metric", ()):
        values.extend([str(getattr(item, "label", "")), str(getattr(item, "value", ""))])
    return values


def _non_expander_values(app) -> list[str]:
    """Collect text from the main tree while skipping collapsed disclosures."""

    values: list[str] = []

    def walk(node: object) -> None:
        if getattr(node, "type", None) == "expander":
            return
        value = getattr(node, "value", None)
        if value is not None:
            values.append(str(value))
        if getattr(node, "type", None) == "metric":
            values.extend(
                [str(getattr(node, "label", "")), str(getattr(node, "value", ""))]
            )
        children = getattr(node, "children", None)
        if isinstance(children, dict):
            for child in children.values():
                walk(child)

    walk(app.main)
    return values


def _primary_labels(app) -> list[str]:
    return [
        button.label
        for button in app.button
        if getattr(button.proto, "type", "secondary") == "primary"
    ]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (AutoRunStatus.IDLE, "待开始"),
        (AutoRunStatus.RUNNING, "正在推进"),
        (AutoRunStatus.WAITING_PROVIDER, "AI 生成中"),
        (AutoRunStatus.WAITING_HUMAN, "等待你的确认"),
        (AutoRunStatus.BLOCKED, "需要处理"),
        (AutoRunStatus.FAILED, "未完成"),
        (AutoRunStatus.SUCCEEDED, "制作完成"),
        (AutoRunStatus.CANCELLED, "已取消"),
    ],
)
def test_status_label_covers_every_run_status(status: AutoRunStatus, expected: str) -> None:
    from aidrama_studio.pages.auto import _STATUS_LABELS, status_label

    assert {item.value for item in AutoRunStatus} <= set(_STATUS_LABELS)
    assert status_label(status) == expected
    assert status_label(status.value) == expected
    assert status.value not in status_label(status)


def test_action_label_covers_every_action_without_leaking_raw_enum() -> None:
    from aidrama_studio.pages.auto import _ACTION_LABELS, action_label

    assert {action.value for action in AutoAction} <= set(_ACTION_LABELS)

    for action in AutoAction:
        label = action_label(action)
        assert label.strip()
        assert action.value not in label
        assert not any(token in label for token in ("PROVIDER", "AUTHORIZATION", "GENERATE_"))

    assert action_label(AutoAction.POLL_EXISTING_TASK) == "检查最新进度"
    assert action_label(AutoAction.PAID_AUTHORIZATION_REQUIRED) == "授权并继续"
    assert action_label("UNRECOGNIZED_INTERNAL_ACTION") == "查看下一步"


@pytest.mark.parametrize(
    ("requested", "stage", "status", "action", "expected_cta", "expected_route"),
    [
        ("APPROVE_STORY", AutoStage.STORY, AutoRunStatus.WAITING_HUMAN, AutoAction.WAITING_HUMAN, "去确认故事", "story"),
        ("APPROVE_SCRIPT", AutoStage.SCRIPT, AutoRunStatus.WAITING_HUMAN, AutoAction.WAITING_HUMAN, "去确认剧本", "story"),
        ("APPROVE_SHOT_PLAN", AutoStage.SHOT_PLAN, AutoRunStatus.WAITING_HUMAN, AutoAction.WAITING_HUMAN, "去确认分镜", "director"),
        ("PROMOTE_BIND_AND_LOCK_REFERENCE", AutoStage.REFERENCES, AutoRunStatus.WAITING_HUMAN, AutoAction.WAITING_HUMAN, "去确认参考资产", "assets"),
        ("APPROVE_OR_REJECT_PRODUCTION_REVIEW", AutoStage.REVIEW, AutoRunStatus.WAITING_HUMAN, AutoAction.WAITING_HUMAN, "前往审片", "review"),
        ("INSPECT_FAILURE_AND_RESUME", AutoStage.PRODUCTION, AutoRunStatus.FAILED, AutoAction.NONE, "检查失败原因", "settings"),
        ("INSPECT_BLOCKER", AutoStage.PRODUCTION, AutoRunStatus.BLOCKED, AutoAction.NONE, "处理阻塞项", "production"),
    ],
)
def test_dominant_action_preserves_formal_human_gate_routes(
    requested: str,
    stage: AutoStage,
    status: AutoRunStatus,
    action: AutoAction,
    expected_cta: str,
    expected_route: str,
) -> None:
    from aidrama_studio.pages.auto import dominant_action

    projection = dominant_action(
        _decision(
            status=status,
            current_stage=stage,
            next_action=action,
            requires_human=True,
            requested_action=requested,
        )
    )
    assert projection.cta == expected_cta
    assert projection.route == expected_route


def test_uncertain_create_out_ranks_legacy_human_flag() -> None:
    from aidrama_studio.pages.auto import dominant_action

    decision = _decision(
        status=AutoRunStatus.BLOCKED,
        current_stage=AutoStage.PRODUCTION,
        next_action=AutoAction.POLL_EXISTING_TASK,
        requires_human=True,
        requested_action="INSPECT_BLOCKER",
        blocking_reason="UNCERTAIN_CREATE",
    )
    projection = dominant_action(decision)
    assert projection.mode == "uncertain"
    assert projection.title == "生成状态需要确认"
    assert projection.cta == "处理生成状态"


def test_unknown_action_is_route_only_and_redacts_endpoint() -> None:
    from aidrama_studio.pages.auto import dominant_action

    decision = {
        "status": AutoRunStatus.RUNNING,
        "current_stage": AutoStage.PRODUCTION,
        "next_action": "FUTURE_INTERNAL_ACTION",
        "why": "内部 endpoint=https://provider.invalid/private",
    }
    projection = dominant_action(decision)
    assert projection.mode == "unknown"
    assert projection.cta == "返回工作台"
    assert projection.route == "dashboard"
    assert "provider.invalid" not in projection.reason


def test_pipeline_projection_is_canonical_and_marks_blocked_stage() -> None:
    from aidrama_studio.pages.auto import pipeline_stage_projections

    decision = _decision(
        status=AutoRunStatus.BLOCKED,
        current_stage=AutoStage.PRODUCTION,
        next_action=AutoAction.POLL_EXISTING_TASK,
        completed=(AutoStage.CREATIVE, AutoStage.STORY),
        blocking_reason="UNCERTAIN_CREATE",
    )
    projections = pipeline_stage_projections(
        decision, keyframe_readiness={"gate": "PASS"}
    )
    # The creator rail has ten visible nodes. The formal Shot First Frame gate
    # is independent from References and precedes Production.
    assert len(projections) == 10

    def field(item: object, name: str) -> object:
        if isinstance(item, dict):
            return item[name]
        return getattr(item, name)

    by_key = {str(field(item, "key")): item for item in projections}
    assert set(by_key) == {
        "CREATIVE",
        "STORY",
        "SCRIPT",
        "SHOT_PLAN",
        "REFERENCES",
        "KEYFRAMES",
        "PRODUCTION",
        "QC",
        "REVIEW",
        "FINAL",
    }
    assert field(by_key[AutoStage.CREATIVE.value], "status") == "COMPLETED"
    assert field(by_key[AutoStage.STORY.value], "status") == "COMPLETED"
    assert field(by_key["KEYFRAMES"], "status") == "COMPLETED"
    assert field(by_key[AutoStage.PRODUCTION.value], "status") == "BLOCKED"
    assert field(by_key[AutoStage.SCRIPT.value], "status") == "PENDING"


def test_keyframe_stage_uses_only_formal_backend_gate() -> None:
    from aidrama_studio.pages.auto import pipeline_stage_projections

    decision = _decision(
        status=AutoRunStatus.RUNNING,
        current_stage=AutoStage.PRODUCTION,
        next_action=AutoAction.CREATE_PRODUCTION_EXECUTION,
        completed=(
            AutoStage.CREATIVE,
            AutoStage.STORY,
            AutoStage.SCRIPT,
            AutoStage.SHOT_PLAN,
            AutoStage.REFERENCES,
        ),
    )
    passed = {
        item.key: item.status
        for item in pipeline_stage_projections(
            decision,
            keyframe_readiness={
                "gate": "PASS",
                "validated_first_frame_count": 3,
            },
        )
    }
    assert passed["KEYFRAMES"] == "COMPLETED"
    assert passed["PRODUCTION"] == "CURRENT"

    blocked = {
        item.key: item.status
        for item in pipeline_stage_projections(
            decision,
            keyframe_readiness={
                "gate": "BLOCKED",
                "missing_first_frame_count": 1,
                "unintended_duplicate_first_frame_count": 1,
            },
        )
    }
    assert blocked["KEYFRAMES"] == "BLOCKED"
    assert blocked["PRODUCTION"] == "PENDING"


def test_pipeline_rail_renders_creator_labels_and_four_state_semantics() -> None:
    decision = _decision(
        status=AutoRunStatus.IDLE,
        current_stage=AutoStage.SCRIPT,
        next_action=AutoAction.GENERATE_SCRIPT,
        completed=(AutoStage.CREATIVE, AutoStage.STORY),
    )
    app = _app_for(decision)
    assert not app.exception
    rail = next(
        str(item.value)
        for item in app.markdown
        if "aidrama-auto-pipeline" in str(item.value)
    )
    for label in ("创意", "故事", "剧本", "参考资产", "镜头首帧 / 视觉预演", "视频制作", "技术质检", "人工审片", "成片"):
        assert label in rail
    for state_label in ("已完成", "当前", "待处理"):
        assert state_label in rail


@pytest.mark.parametrize(
    ("name", "decision", "cta"),
    [
        (
            "fresh",
            _decision(
                status=AutoRunStatus.IDLE,
                current_stage=AutoStage.STORY,
                next_action=AutoAction.GENERATE_OR_CREATE_STORY,
                why="等待创意输入。",
            ),
            "开始自动制作",
        ),
        (
            "story_gate",
            _decision(
                status=AutoRunStatus.WAITING_HUMAN,
                current_stage=AutoStage.STORY,
                next_action=AutoAction.WAITING_HUMAN,
                requires_human=True,
                requested_action="APPROVE_STORY",
                why="AI 已完成故事，等待你的确认。",
            ),
            "去确认故事",
        ),
        (
            "script_gate",
            _decision(
                status=AutoRunStatus.WAITING_HUMAN,
                current_stage=AutoStage.SCRIPT,
                next_action=AutoAction.WAITING_HUMAN,
                requires_human=True,
                requested_action="APPROVE_SCRIPT",
                why="AI 已完成剧本，等待你的确认。",
            ),
            "去确认剧本",
        ),
        (
            "shot_gate",
            _decision(
                status=AutoRunStatus.WAITING_HUMAN,
                current_stage=AutoStage.SHOT_PLAN,
                next_action=AutoAction.WAITING_HUMAN,
                requires_human=True,
                requested_action="APPROVE_SHOT_PLAN",
                why="AI 已完成分镜，等待你的确认。",
            ),
            "去确认分镜",
        ),
        (
            "reference_gate",
            _decision(
                status=AutoRunStatus.WAITING_HUMAN,
                current_stage=AutoStage.REFERENCES,
                next_action=AutoAction.WAITING_HUMAN,
                requires_human=True,
                requested_action="PROMOTE_BIND_AND_LOCK_REFERENCE",
                why="参考资产需要你的确认。",
            ),
            "去确认参考资产",
        ),
        (
            "reference_bind_gate",
            _decision(
                status=AutoRunStatus.WAITING_HUMAN,
                current_stage=AutoStage.REFERENCES,
                next_action=AutoAction.WAITING_HUMAN,
                requires_human=True,
                requested_action="BIND_AND_LOCK_REFERENCE",
                why="参考资产版本需要你的确认。",
            ),
            "去确认参考资产",
        ),
        (
            "paid_gate",
            _decision(
                status=AutoRunStatus.WAITING_HUMAN,
                current_stage=AutoStage.PRODUCTION,
                next_action=AutoAction.PAID_AUTHORIZATION_REQUIRED,
                requires_paid=True,
                why="视频制作需要明确的付费授权。",
            ),
            "授权并继续",
        ),
        (
            "provider_wait",
            _decision(
                status=AutoRunStatus.WAITING_PROVIDER,
                current_stage=AutoStage.PRODUCTION,
                next_action=AutoAction.POLL_EXISTING_TASK,
                why="视频任务正在后台生成。",
            ),
            "检查最新进度",
        ),
        (
            "uncertain_create",
            _decision(
                status=AutoRunStatus.BLOCKED,
                current_stage=AutoStage.PRODUCTION,
                next_action=AutoAction.POLL_EXISTING_TASK,
                blocking_reason="UNCERTAIN_CREATE",
                why="生成状态需要确认。",
            ),
            "处理生成状态",
        ),
        (
            "human_review",
            _decision(
                status=AutoRunStatus.WAITING_HUMAN,
                current_stage=AutoStage.REVIEW,
                next_action=AutoAction.WAITING_HUMAN,
                requires_human=True,
                requested_action="APPROVE_OR_REJECT_PRODUCTION_REVIEW",
                why="制作完成，等待人工审片。",
            ),
            "前往审片",
        ),
        (
            "success",
            _decision(
                status=AutoRunStatus.SUCCEEDED,
                current_stage=AutoStage.COMPLETED,
                next_action=AutoAction.NONE,
                completed=tuple(AutoStage),
                why="流程已完成。",
            ),
            "查看成片",
        ),
    ],
)
def test_each_boundary_state_has_one_dominant_cta(name: str, decision: AutoDecision, cta: str) -> None:
    app = _app_for(decision)
    assert not app.exception, f"{name}: {app.exception}"
    primary = _primary_labels(app)
    assert primary == [cta], f"{name}: primary buttons were {primary!r}"


def test_workbench_hero_preserves_project_truth_and_hides_raw_ids() -> None:
    decision = _decision(
        status=AutoRunStatus.WAITING_PROVIDER,
        current_stage=AutoStage.PRODUCTION,
        next_action=AutoAction.POLL_EXISTING_TASK,
        why="后台任务已保存，可安全离开页面。",
        metadata={
            "provider_task_id": "provider-task-secret-123",
            "execution_uuid": "0123456789abcdef0123456789abcdef",
            "endpoint": "https://provider.invalid/private",
        },
    )
    app = _app_for(decision)
    assert not app.exception
    visible = "\n".join(_all_visible_values(app))
    assert "AI 制作台" in visible
    assert PROJECT_TITLE in visible
    assert str(PROJECT_DURATION) in visible
    assert PROJECT_ASPECT in visible
    assert "120" in visible
    assert "60 秒" not in visible
    assert "provider-task-secret-123" not in visible
    assert "0123456789abcdef0123456789abcdef" not in visible
    assert "provider.invalid" not in visible


@pytest.mark.parametrize(
    ("duration", "expected"),
    [(120, "120 秒"), (600, "600 秒"), (3600, "3600 秒"), (1.5, "1.5 秒")],
)
def test_duration_projection_preserves_legal_target_values(duration: object, expected: str) -> None:
    from aidrama_studio.pages.auto import _duration_text

    assert _duration_text(SimpleNamespace(target_duration_seconds=duration)) == expected


def test_advanced_event_projection_does_not_leak_engineering_ids_to_normal_copy() -> None:
    decision = _decision(
        status=AutoRunStatus.WAITING_PROVIDER,
        current_stage=AutoStage.PRODUCTION,
        next_action=AutoAction.POLL_EXISTING_TASK,
        why="后台任务已保存。",
    )
    app = _app_for(
        decision,
        events=[
            {
                "action": "CREATE_PRODUCTION_EXECUTION",
                "result": "provider-task-secret-456",
                "reason": "endpoint=https://provider.invalid/private",
                "timestamp": "2026-08-28T00:00:00+00:00",
                "actor": "internal-worker-uuid-789",
            }
        ],
    )
    assert not app.exception
    # AppTest's flattened element lists include expander children even when
    # collapsed, so walk the main tree and explicitly skip disclosure nodes.
    visible = "\n".join(_non_expander_values(app))
    assert "provider-task-secret-456" not in visible
    assert "provider.invalid" not in visible
    assert "internal-worker-uuid-789" not in visible
    advanced = [item for item in app.expander if "AI 决策记录" in item.label]
    assert advanced and advanced[0].proto.expanded is False


def test_normal_gate_copy_redacts_identifier_key_value_pairs() -> None:
    """Provider/execution/artifact identifiers stay out of creator copy."""

    decision = _decision(
        status=AutoRunStatus.WAITING_HUMAN,
        current_stage=AutoStage.REVIEW,
        next_action=AutoAction.WAITING_HUMAN,
        requires_human=True,
        requested_action="APPROVE_OR_REJECT_PRODUCTION_REVIEW",
        why=(
            "provider_task_id=provider-task-secret-999 "
            "execution_uuid=execution-secret-888 "
            "artifact_uuid=artifact-secret-777"
        ),
    )
    app = _app_for(decision)
    assert not app.exception
    visible = "\n".join(_non_expander_values(app))
    assert "provider-task-secret-999" not in visible
    assert "execution-secret-888" not in visible
    assert "artifact-secret-777" not in visible


def test_paid_gate_keeps_exact_budget_contract_and_does_not_grant_on_load() -> None:
    decision = _decision(
        status=AutoRunStatus.WAITING_HUMAN,
        current_stage=AutoStage.PRODUCTION,
        next_action=AutoAction.PAID_AUTHORIZATION_REQUIRED,
        requires_paid=True,
        why="本次视频制作需要付费授权。",
    )
    app = _app_for(
        decision,
        paid_preview={
            "required_create_count": 8,
            "per_item_max": 1,
            "retry_limit": 0,
            "provider_label": "已配置的视频方案",
            "authorization_fingerprint": "c" * 64,
        },
    )
    assert not app.exception
    labels = _all_visible_values(app)
    assert any("预计创建" in value for value in labels)
    assert any("单项上限" in value for value in labels)
    assert any("自动重试" in value for value in labels)
    assert "8" in labels
    assert "1" in labels
    assert "0" in labels
    assert any("我确认仅授权以上精确范围" in item.label for item in app.checkbox)
    primary = _primary_labels(app)
    assert primary == ["授权并继续"]
    # A checkbox must gate the primary action.  AppTest exposes the disabled
    # state through the protobuf, and no grant call is possible in this run.
    grant_buttons = [item for item in app.button if item.label == "授权并继续"]
    assert len(grant_buttons) == 1
    assert getattr(grant_buttons[0].proto, "disabled", False) is True


@pytest.mark.parametrize(
    "invalid_field",
    [
        {"per_item_max": 2},
        {"retry_limit": 1},
        {"authorization_fingerprint": "not-a-sha256"},
        {"required_create_count": 0},
    ],
)
def test_paid_gate_fails_closed_on_preview_contract_drift(
    invalid_field: dict[str, object],
) -> None:
    decision = _decision(
        status=AutoRunStatus.WAITING_HUMAN,
        current_stage=AutoStage.PRODUCTION,
        next_action=AutoAction.PAID_AUTHORIZATION_REQUIRED,
        requires_paid=True,
    )
    preview = {
        "required_create_count": 8,
        "per_item_max": 1,
        "retry_limit": 0,
        "provider_label": "已配置的视频方案",
        "authorization_fingerprint": "d" * 64,
    }
    preview.update(invalid_field)
    app = _app_for(decision, paid_preview=preview)
    assert not app.exception
    assert app.error
    assert any("没有发起生成请求" in item.value for item in app.error)
    assert not _primary_labels(app)


def test_waiting_provider_copy_forbids_new_submission_language() -> None:
    decision = _decision(
        status=AutoRunStatus.WAITING_PROVIDER,
        current_stage=AutoStage.PRODUCTION,
        next_action=AutoAction.POLL_EXISTING_TASK,
        why="任务状态已保存，可以安全离开页面。",
    )
    app = _app_for(decision)
    assert not app.exception
    visible = "\n".join(_all_visible_values(app))
    assert "AI 正在生成" in visible
    assert "检查最新进度" in visible
    assert "轮询现有任务" not in visible
    assert "重新生成" not in visible


def test_uncertain_create_is_recovery_only_and_never_says_regenerate() -> None:
    decision = _decision(
        status=AutoRunStatus.BLOCKED,
        current_stage=AutoStage.PRODUCTION,
        next_action=AutoAction.POLL_EXISTING_TASK,
        blocking_reason="RECONCILIATION_REQUIRED",
        why="生成状态需要确认；不会再次提交生成请求。",
    )
    app = _app_for(decision)
    assert not app.exception
    visible = "\n".join(_all_visible_values(app))
    assert "生成状态需要确认" in visible
    assert "不会再次提交生成请求" in visible
    assert "处理生成状态" in _primary_labels(app)
    assert "重新生成" not in visible


def test_capability_summary_is_provider_neutral_and_settings_is_secondary() -> None:
    decision = _decision(
        status=AutoRunStatus.IDLE,
        current_stage=AutoStage.STORY,
        next_action=AutoAction.GENERATE_OR_CREATE_STORY,
    )
    app = _app_for(decision)
    assert not app.exception
    visible = "\n".join(_all_visible_values(app))
    assert "AI 能力" in visible
    assert "文本生成" in visible or "创作 AI" in visible
    assert "参考图生成" in visible or "参考图" in visible
    assert "视频生成" in visible
    assert "endpoint" not in visible.casefold()
    assert "manifest" not in visible.casefold()


def test_missing_llm_or_video_capability_offers_settings_without_competing_primary_cta() -> None:
    decision = _decision(
        status=AutoRunStatus.IDLE,
        current_stage=AutoStage.STORY,
        next_action=AutoAction.GENERATE_OR_CREATE_STORY,
    )
    missing = {
        key: {
            "capability": key,
            "state": "UNAVAILABLE" if key in {"text", "video"} else "READY",
            "configured": key not in {"text", "video"},
            "verified": key not in {"text", "video"},
            "runtime_available": key not in {"text", "video"},
        }
        for key in ("text", "image", "video", "vision", "tts")
    }
    app = _app_for(decision, capabilities=missing)
    assert not app.exception
    assert any(item.label == "去设置 AI 模型" for item in app.button)
    assert _primary_labels(app) == ["开始自动制作"]


def test_success_panel_keeps_final_and_production_routes_distinct() -> None:
    decision = _decision(
        status=AutoRunStatus.SUCCEEDED,
        current_stage=AutoStage.COMPLETED,
        next_action=AutoAction.NONE,
        completed=tuple(AutoStage),
    )
    app = _app_for(decision)
    assert not app.exception
    assert "查看成片" in [item.label for item in app.button]
    assert "查看制作详情" in [item.label for item in app.button]
    assert _primary_labels(app) == ["查看成片"]


def test_navigation_routes_and_legacy_aliases_remain_stable() -> None:
    from aidrama_studio.components.navigation import PAGE_DEFINITIONS, canonical_page_key

    expected_keys = {
        "dashboard",
        "auto",
        "creative",
        "story",
        "assets",
        "director",
        "production",
        "review",
        "postproduction",
        "settings",
    }
    definitions = {key: (title, url_path) for key, title, url_path, _render in PAGE_DEFINITIONS}
    assert set(definitions) == expected_keys
    assert all(url_path == key for key, (_title, url_path) in definitions.items())
    assert definitions["auto"][0] == "AI 制作台"
    assert definitions["story"][0] == "故事与剧本"

    aliases = {
        "workbench": "dashboard",
        "creative-intake": "creative",
        "creative_intake": "creative",
        "story-script": "story",
        "story_script": "story",
        "references": "assets",
        "storyboard": "director",
        "final": "postproduction",
        "post": "postproduction",
    }
    for alias, canonical in aliases.items():
        assert canonical_page_key(alias) == canonical
    assert canonical_page_key("/AUTO/") == "auto"
    assert canonical_page_key("") is None
    assert canonical_page_key(None) is None


def test_request_navigation_preserves_project_query_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    from aidrama_studio.components import navigation

    session_state: dict[str, object] = {"current_project_id": PROJECT_ID}
    query_params: dict[str, str] = {}

    class _Rerun(Exception):
        pass

    monkeypatch.setattr(
        navigation,
        "st",
        SimpleNamespace(
            session_state=session_state,
            query_params=query_params,
            rerun=lambda: (_ for _ in ()).throw(_Rerun()),
        ),
    )
    with pytest.raises(_Rerun):
        navigation.request_navigation("/final/")
    assert session_state["_aidrama_next_page"] == "postproduction"
    assert query_params["project"] == PROJECT_ID
