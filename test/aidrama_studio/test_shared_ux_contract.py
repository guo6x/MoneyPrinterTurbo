"""Focused contracts for the Creative Workspace shared shell.

These tests stay at the UI projection boundary.  They do not initialize a
provider, contact a runtime, or depend on a live Streamlit session.
"""

from __future__ import annotations

from types import SimpleNamespace

from aidrama_studio.components.navigation import (
    PAGE_ALIASES,
    PAGE_DEFINITIONS,
    canonical_page_key,
)
from aidrama_studio.domain import ProjectStatus
from aidrama_studio.pages._shared import (
    normalize_activity_snapshots,
    normalize_capability_snapshot,
    normalize_capability_snapshots,
    workflow_stage_projection,
)


def test_navigation_exposes_single_creative_and_story_routes_in_target_order():
    keys = [item[0] for item in PAGE_DEFINITIONS]
    assert keys[:3] == ["dashboard", "creative", "story"]
    assert keys[-1] == "settings"
    assert keys.count("creative") == 1
    assert canonical_page_key("creative-intake") == "creative"
    assert canonical_page_key("story-script") == "story"
    assert PAGE_ALIASES["storyboard"] == "director"


def test_workflow_projection_prefers_canonical_service_over_project_status():
    calls: list[str] = []

    class CanonicalState:
        def workflow_stage(self, project_id: str):
            calls.append(project_id)
            return ProjectStatus.STORY

    project = SimpleNamespace(id="project-1", status=ProjectStatus.DRAFT)
    projection = workflow_stage_projection(project, state_service=CanonicalState())

    assert calls == ["project-1"]
    assert projection.canonical is True
    assert projection.status is ProjectStatus.STORY
    assert projection.label == "故事 / 剧本"
    assert projection.next_page == "story"


def test_workflow_projection_marks_degraded_read_without_using_row_status():
    class BrokenState:
        def workflow_stage(self, _project_id: str):
            raise RuntimeError("unavailable")

    project = {"id": "project-2", "status": ProjectStatus.POSTPRODUCTION}
    projection = workflow_stage_projection(project, state_service=BrokenState())

    assert projection.canonical is False
    assert projection.status is ProjectStatus.DRAFT
    assert projection.label == "创意"
    assert projection.next_page == "creative"
    assert projection.diagnostic


def test_capability_normalization_keeps_video_alias_and_authorization_gate():
    source = {
        "VIDEO_GENERATIVE": {
            "state": "READY",
            "ready": True,
            "configured": True,
            "verified": True,
            "runtime_available": True,
            "authorization_required": True,
            "create_authorized": False,
            "model": "safe-display-model",
        }
    }
    snapshots = normalize_capability_snapshots(source)
    video = next(item for item in snapshots if item.capability == "VIDEO")

    assert video.capability == "VIDEO"
    assert video.model_or_profile == "safe-display-model"
    assert video.state == "needs_confirmation"
    assert video.ready is False
    assert video.authorization_required is True
    assert video.create_authorized is False


def test_malformed_readiness_payloads_fail_closed():
    contradictory = normalize_capability_snapshot(
        {"capability": "video", "state": "UNAVAILABLE", "ready": True}
    )
    unknown = normalize_capability_snapshot(
        {
            "capability": "vision",
            "state": "UNRECOGNIZED",
            "configured": True,
            "verified": True,
            "runtime_available": True,
        }
    )

    assert contradictory.state == "error"
    assert unknown.state == "error"
    assert contradictory.runtime_available is False
    assert unknown.runtime_available is False


def test_activity_normalization_hides_idle_and_never_invents_progress():
    activities = normalize_activity_snapshots(
        [
            {
                "id": "activity-1",
                "title": "生成候选图",
                "state": "running",
                "progress": 1.5,
            },
            {"title": "已完成", "state": "idle", "progress": 0.9},
        ]
    )

    assert len(activities) == 1
    assert activities[0].state == "running"
    assert activities[0].progress == 1.0
    assert "activity_id" not in activities[0].as_public_dict()


def test_project_context_demotes_shell_cta_when_page_supplies_local_action():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.domain import ProjectStatus
from aidrama_studio.pages import _shared

class State:
    def workflow_stage(self, _project_id):
        return ProjectStatus.STORY

_shared.render_project_context(
    SimpleNamespace(id="project-cta", title="CTA"),
    next_action="保存修改",
    next_page="story",
    state_service=State(),
)
"""
    ).run()

    assert not app.exception
    button = next(item for item in app.button if item.label == "保存修改")
    assert button.proto.type == "secondary"


def test_project_context_keeps_canonical_next_step_primary_without_local_cta():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.domain import ProjectStatus
from aidrama_studio.pages import _shared

class State:
    def workflow_stage(self, _project_id):
        return ProjectStatus.DRAFT

_shared.render_project_context(
    SimpleNamespace(id="project-canonical-cta", title="CTA"),
    state_service=State(),
)
"""
    ).run()

    assert not app.exception
    button = next(item for item in app.button if item.label == "开始创作")
    assert button.proto.type == "primary"


def test_automation_mode_defaults_to_collaboration_and_lists_hard_stop_gates():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        """
from aidrama_studio.pages._shared import render_automation_mode
render_automation_mode('project-automation')
"""
    ).run()

    assert not app.exception
    assert app.radio[0].value == "协作模式"
    app.radio[0].set_value("连续生成").run()
    copy = "\n".join(str(item.value) for item in app.caption)
    for gate in ("故事 / 剧本确认", "参考锁定", "付费任务", "人工审片", "最终导出"):
        assert gate in copy


def test_storyboard_shot_count_is_dynamic_and_responsive_director_contract_is_mounted():
    from pathlib import Path
    from streamlit.testing.v1 import AppTest

    from aidrama_studio.pages import director

    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages.director import _shot_metrics
project = SimpleNamespace(target_duration_seconds=20)
plan = SimpleNamespace(shots=[
    SimpleNamespace(duration_seconds=3, status='DRAFT', risk_level='LOW'),
    SimpleNamespace(duration_seconds=4, status='LOCKED', risk_level='LOW'),
    SimpleNamespace(duration_seconds=5, status='DRAFT', risk_level='HIGH'),
])
_shot_metrics(project, plan)
"""
    ).run()

    assert not app.exception
    metrics = {item.label: str(item.value) for item in app.metric}
    assert metrics["镜头数"] == "3"
    source = Path(director.__file__).read_text(encoding="utf-8")
    css = (Path(director.__file__).parents[1] / "styles.css").read_text(encoding="utf-8")
    assert "aidrama-storyboard-workstation-marker" in source
    assert 'st.expander("AI 导演", expanded=False)' in source
    assert "@media (max-width: 1400px)" in css
    assert "@media (min-width: 1500px)" in css
