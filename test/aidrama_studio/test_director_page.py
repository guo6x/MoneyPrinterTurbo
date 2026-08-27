from __future__ import annotations

from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from aidrama_studio.domain import (
    DirectorDecisionStatus,
    DirectorRecommendation,
)
from aidrama_studio.pages import director as page


def _decision(status: DirectorDecisionStatus):
    return SimpleNamespace(
        id="decision-1",
        status=status,
        project_state="REFERENCES",
        recommendation=DirectorRecommendation(
            action="LOCK_LOCATION_REFERENCE",
            reason="生产准备缺少已锁定的场景参考资产",
            requires_human_approval=True,
        ),
    )


def _render_with_decision(status: DirectorDecisionStatus):
    return AppTest.from_string(
        f"""
from types import SimpleNamespace
from aidrama_studio.domain import DirectorDecisionStatus, DirectorSessionStatus, DirectorRecommendation
from aidrama_studio.pages import director as page

status = DirectorDecisionStatus.{status.name}
decision = SimpleNamespace(
    id='decision-1', status=status, project_state='REFERENCES',
    recommendation=DirectorRecommendation(
        action='LOCK_LOCATION_REFERENCE',
        reason='生产准备缺少已锁定的场景参考资产',
        requires_human_approval=True,
    ),
)
session = SimpleNamespace(id='session-1', status=DirectorSessionStatus.ACTIVE, pending_recommendation=None)
project = SimpleNamespace(id='project-1', title='Director UI project')

class FakeDirector:
    def inspect_project(self, project_id):
        return {{
            'project_state': 'REFERENCES',
            'readiness': {{'ready': False, 'blocked_reasons': ['missing reference']}},
            'qc_failures': [],
        }}
    def list_sessions(self, project_id): return [session]
    def list_decisions(self, project_id, session_id): return [decision]

class FakeProducer:
    def recommendations(self, project_id): return [decision.recommendation]
    def high_risk_shots(self, project_id): return []

page.current_project_or_stop = lambda: project
page.DirectorService = FakeDirector
page.ProducerService = FakeProducer
page._render_director_console(project)
"""
    ).run(timeout=30)


def test_decision_status_labels_are_user_facing():
    assert page._decision_status_label(SimpleNamespace(status=DirectorDecisionStatus.APPROVED)) == "已批准"
    assert page._decision_status_label(SimpleNamespace(status=DirectorDecisionStatus.REJECTED)) == "已拒绝"
    assert page._decision_status_label(SimpleNamespace(status=DirectorDecisionStatus.COMPLETED)) == "已完成"


def test_approved_decision_is_visible_as_approved_and_resumable():
    app = _render_with_decision(DirectorDecisionStatus.APPROVED)
    assert not app.exception
    assert any("最近建议已批准" in item.value for item in app.success)
    assert any(button.label == "继续分析" for button in app.button)
    assert any("已批准" in item.value for item in app.markdown)


def test_rejected_decision_is_visible_as_rejected_without_auto_action():
    app = _render_with_decision(DirectorDecisionStatus.REJECTED)
    assert not app.exception
    assert any("最近建议已拒绝" in item.value for item in app.warning)
    assert any("已拒绝" in item.value for item in app.markdown)


def test_outdated_plan_can_fork_from_current_approved_script_without_mutating_history():
    app = AppTest.from_string(
        """
import streamlit as st
from types import SimpleNamespace
from aidrama_studio.pages import director as page

project = SimpleNamespace(id='project-1', target_duration_seconds=60)
approved = {'id': 'script-v2', 'version': 2}
old_plan = {
    'id': 'plan-v1',
    'source_script_revision_id': 'script-v1',
    'status': 'APPROVED',
    'shots': [],
}

class FakeShotService:
    def create_manual_plan(self, project, script):
        st.session_state['created_plan_source'] = script['id']
        return {'id': 'plan-v2'}

page._render_storyboard_board(
    project,
    FakeShotService(),
    [old_plan],
    old_plan,
    approved,
)
"""
    ).run(timeout=30)

    assert not app.exception
    assert any("当前分镜来自旧版结构化剧本" in item.value for item in app.warning)
    button = next(
        item for item in app.button if item.label == "基于当前剧本创建新分镜草稿"
    )

    app = button.click().run(timeout=30)

    assert not app.exception
    assert app.session_state["created_plan_source"] == "script-v2"
    assert app.session_state["director-plan-project-1"] == "plan-v2"
