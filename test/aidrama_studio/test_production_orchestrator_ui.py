from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from aidrama_studio.pages import production as page


def _job(status="READY"):
    return SimpleNamespace(id="job-1", project_id="project-1", status=status, created_at="now", updated_at="now", shot_plan_revision_id="plan-1")


def _shot(shot_id, order, status="PENDING"):
    return SimpleNamespace(id=f"ps-{shot_id}", shot_id=shot_id, order_index=order, status=status, production_job_id="job-1")


def _execution(shot_id, status="SUCCEEDED"):
    snapshot = SimpleNamespace(shot_parameters={shot_id: {"visual_intent": shot_id}}, story_revision_id="story-1", script_revision_id="script-1", shot_plan_revision_id="plan-1")
    return SimpleNamespace(id=f"execution-{shot_id}", status=status, worker_type="mpt", created_at="now", input_snapshot=snapshot)


def test_director_console_has_required_user_facing_sections():
    source = Path(page.__file__).read_text(encoding="utf-8")
    for label in ("制作准备度", "开始整剧制作", "继续制作", "停止制作", "镜头生产 Board", "当前镜头", "QC未通过", "人审拒绝", "高级信息 / 调试信息"):
        assert label in source
    assert "Traceback (most recent call last)" in source
    assert "生成视频" not in source


def test_shot_board_exposes_source_candidates_and_explicit_creative_actions():
    source = Path(page.__file__).read_text(encoding="utf-8")
    for label in ("Shot sources", "Current decision", "Source decision history", "Promote Preview", "Regenerate creative attempt"):
        assert label in source
    # The page must delegate retries with the frozen snapshot; it must not
    # construct a new ProductionInputSnapshot or call a provider directly.
    assert "request_creative_regeneration" in source
    assert "The existing immutable snapshot is passed through unchanged" in source


def test_default_flow_precedes_technical_job_controls():
    source = Path(page.__file__).read_text(encoding="utf-8")
    readiness = source.index("_render_readiness_console(readiness)")
    primary = source.index("_render_primary_action(", readiness)
    board = source.index("_render_shot_board(", primary)
    advanced = source.index('st.expander("高级信息 / 调试信息"', board)
    assert readiness < primary < board < advanced


def test_ready_page_keeps_job_controls_collapsed_and_primary_director_action():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages import production as page
project = SimpleNamespace(id='project-1', title='Ready project')
class Production:
    def validate_job_readiness(self, project_id, revision_id=None):
        return {'ready': True, 'blocked_reasons': [], 'shot_count': 3, 'story_bible_approved': True, 'structured_script_approved': True, 'shot_plan_approved': True, 'character_reference_coverage': '1/1', 'location_reference_coverage': '1/1'}
    def list_jobs(self, project_id):
        return []
    def create_production_job(self, project_id):
        return SimpleNamespace(id='job-created', status='READY', shot_plan_revision_id='plan')
class Execution:
    def __init__(self, **kwargs):
        pass
page.current_project_or_stop = lambda: project
page.ProductionService = Production
page.ProductionExecutionService = Execution
page.render()
"""
    ).run()
    assert not app.exception
    assert any(button.label == "开始整剧制作" and not button.disabled for button in app.button)
    assert app.expander and app.expander[0].label == "高级信息 / 调试信息"
    source = Path(page.__file__).read_text(encoding="utf-8")
    assert 'expanded=bool(st.session_state.get(advanced_key))' in source


def test_multishot_board_orders_by_canonical_order_and_renders_statuses():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages import production as page
project = SimpleNamespace(id='project-1')
job = SimpleNamespace(id='job-1', status='RUNNING')
def shot(sid, order, status='PENDING'):
    return SimpleNamespace(id='ps-'+sid, shot_id=sid, order_index=order, status=status)
class Production:
    def get_shot_board(self, project_id, job_id):
        return [
            {'production_shot': shot('shot-2', 2), 'scene_id': 'scene-2', 'scene_name': 'Second', 'description': 'second'},
            {'production_shot': shot('shot-1', 1, 'RUNNING'), 'scene_id': 'scene-1', 'scene_name': 'First', 'description': 'first'},
        ]
class Execution:
    def list_executions(self, project_id, job_id):
        def execution(sid, status):
            snapshot = SimpleNamespace(shot_parameters={sid: {}}, story_revision_id='story', script_revision_id='script', shot_plan_revision_id='plan')
            return SimpleNamespace(id='ex-'+sid, status=status, input_snapshot=snapshot)
        return [execution('shot-1', 'RUNNING'), execution('shot-2', 'SUCCEEDED')]
class QC:
    def list_results(self, project_id, execution_id):
        return []
page._render_shot_board(Production(), Execution(), QC(), project, job, {'total_shots': 2, 'completed_shots': 1, 'percent_complete': 50, 'current_shot_id': 'shot-1'})
"""
    ).run()
    assert not app.exception
    rendered = "\n".join(item.value for item in app.markdown)
    assert rendered.index("First") < rendered.index("Second")
    assert "当前镜头" in "\n".join(item.value for item in app.info)
    assert any("制作中" in item.value for item in app.markdown)


def test_qc_failed_and_rejected_review_are_visible():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages import production as page
project = SimpleNamespace(id='project-1')
job = SimpleNamespace(id='job-1', status='FAILED')
shot = SimpleNamespace(id='ps-1', shot_id='shot-1', order_index=1, status='SUCCEEDED')
snapshot = SimpleNamespace(shot_parameters={'shot-1': {}}, story_revision_id='story', script_revision_id='script', shot_plan_revision_id='plan')
execution = SimpleNamespace(id='ex-1', status='SUCCEEDED', input_snapshot=snapshot)
result = SimpleNamespace(id='qc-1', status='QC_FAILED', summary_json={'failed_metrics': ['audio_stream']})
class Production:
    def get_shot_board(self, project_id, job_id):
        return [{'production_shot': shot, 'scene_id': 'scene-1', 'scene_name': 'Scene', 'description': 'desc'}]
class Execution:
    def list_executions(self, project_id, job_id):
        return [execution]
    def list_events(self, project_id, execution_id):
        return []
class QC:
    def list_results(self, project_id, execution_id):
        return [result]
    def list_reviews(self, project_id, result_id):
        return [SimpleNamespace(decision='REJECTED')]
page._render_shot_board(Production(), Execution(), QC(), project, job, {'total_shots': 1, 'completed_shots': 0, 'percent_complete': 0, 'current_shot_id': None})
"""
    ).run()
    assert not app.exception
    assert any("QC 未通过" in item.value for item in app.error)
    assert any("人审拒绝" in item.value for item in app.warning)


def test_runtime_failed_shot_renders_sanitized_reason():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages import production as page
project = SimpleNamespace(id='project-1')
job = SimpleNamespace(id='job-1', status='FAILED')
shot = SimpleNamespace(id='ps-1', shot_id='shot-1', order_index=1, status='FAILED')
snapshot = SimpleNamespace(shot_parameters={'shot-1': {}}, story_revision_id='story', script_revision_id='script', shot_plan_revision_id='plan')
execution = SimpleNamespace(id='ex-1', status='FAILED', input_snapshot=snapshot)
event = SimpleNamespace(event_type='FAILED', payload_json={'error': 'adapter unavailable'}, created_at='now')
class Production:
    def get_shot_board(self, project_id, job_id):
        return [{'production_shot': shot, 'scene_id': 'scene-1', 'scene_name': 'Scene', 'description': 'desc'}]
class Execution:
    def list_executions(self, project_id, job_id):
        return [execution]
    def list_events(self, project_id, execution_id):
        return [event]
class QC:
    def list_results(self, project_id, execution_id):
        return []
page._render_shot_board(Production(), Execution(), QC(), project, job, {'total_shots': 1, 'completed_shots': 0, 'failed_shots': 1, 'pending_shots': 0, 'percent_complete': 0, 'current_shot_id': 'shot-1'})
"""
    ).run()
    assert not app.exception
    assert any("镜头制作失败" in item.value and "adapter unavailable" in item.value for item in app.error)


def test_completed_qc_pass_shot_renders_pass_state():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages import production as page
project = SimpleNamespace(id='project-1')
job = SimpleNamespace(id='job-1', status='RUNNING')
shot = SimpleNamespace(id='ps-1', shot_id='shot-1', order_index=1, status='SUCCEEDED')
snapshot = SimpleNamespace(shot_parameters={'shot-1': {}}, story_revision_id='story', script_revision_id='script', shot_plan_revision_id='plan')
execution = SimpleNamespace(id='ex-1', status='SUCCEEDED', input_snapshot=snapshot)
result = SimpleNamespace(id='qc-1', status='QC_PASS', summary_json={'passed': 1, 'failed': 0, 'skipped': 0})
class Production:
    def get_shot_board(self, project_id, job_id):
        return [{'production_shot': shot, 'scene_id': 'scene-1', 'scene_name': 'Scene', 'description': 'desc'}]
class Execution:
    def list_executions(self, project_id, job_id):
        return [execution]
class QC:
    def list_results(self, project_id, execution_id):
        return [result]
    def list_reviews(self, project_id, result_id):
        return []
page._render_shot_board(Production(), Execution(), QC(), project, job, {'total_shots': 1, 'completed_shots': 1, 'failed_shots': 0, 'pending_shots': 0, 'percent_complete': 100, 'current_shot_id': None})
"""
    ).run()
    assert not app.exception
    assert any("QC通过" in item.value for item in app.markdown)


def test_primary_action_uses_resume_and_cancellation_services(monkeypatch):
    project = SimpleNamespace(id="project-1")
    job = _job("RUNNING")
    calls = []

    class Orchestrator:
        def get_job_progress(self, *args):
            return {"completed_shots": 1}

        def resume_job(self, *args, **kwargs):
            calls.append("resume")

        def cancel_job(self, *args, **kwargs):
            calls.append("cancel")

        def run_job(self, *args, **kwargs):
            calls.append("run")

    labels = {"刷新制作状态": False, "停止制作": True}
    monkeypatch.setattr(page.st, "button", lambda label, **kwargs: labels.get(label, False))
    monkeypatch.setattr(page.st, "columns", lambda count: [SimpleNamespace(button=lambda label, **kwargs: labels.get(label, False)) for _ in range(count)])
    monkeypatch.setattr(page.st, "rerun", lambda: None)
    page._render_primary_action(Orchestrator(), None, project, job, {"ready": True}, {"completed_shots": 1})
    assert calls == ["cancel"]


def test_interrupted_primary_action_invokes_resume(monkeypatch):
    calls = []

    class Orchestrator:
        def resume_job(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(page.st, "button", lambda label, **kwargs: label == "继续制作")
    monkeypatch.setattr(page.st, "rerun", lambda: None)
    page._render_primary_action(
        Orchestrator(), None, SimpleNamespace(id="project-1"), _job("CANCELLED"), {"ready": True}, {"completed_shots": 2}
    )
    assert calls and calls[0][0][0] == "project-1" and calls[0][0][1] == "job-1"


def test_ready_primary_action_creates_job_then_runs_canonical_orchestrator(monkeypatch):
    calls = []
    job = _job("READY")

    class Orchestrator:
        def run_job(self, *args, **kwargs):
            calls.append(("run", args))

    def ensure_job():
        calls.append(("create",))
        return job

    monkeypatch.setattr(page.st, "button", lambda label, **kwargs: label == "开始整剧制作")
    monkeypatch.setattr(page.st, "rerun", lambda: None)
    page._render_primary_action(
        Orchestrator(), None, SimpleNamespace(id="project-1"), None, {"ready": True}, {}, ensure_job=ensure_job
    )
    assert calls == [("create",), ("run", ("project-1", "job-1"))]


def test_blocked_primary_action_is_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(page.st, "button", lambda label, **kwargs: calls.append((label, kwargs.get("disabled"))) or False)
    page._render_primary_action(object(), None, SimpleNamespace(id="p"), _job("DRAFT"), {"ready": False}, {})
    assert calls == [("开始整剧制作", True)]


def test_paid_authorization_renders_region_reference_count_and_cloud_disclosure():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages import production as page

project = SimpleNamespace(id='project-1')
job = SimpleNamespace(id='job-1', status='READY')

class Orchestrator:
    def list_provider_options(self, project_id):
        return ({
            'provider_id': 'CN_VIDEO',
            'model_id': 'video-model-v1',
            'endpoint_profile_id': 'cn-video-endpoint',
            'deployment_region': 'MAINLAND_CHINA',
            'endpoint_class': 'CN_VIDEO_PUBLIC',
            'available': True,
            'verified': False,
        },)
    def preview_authorization(self, project_id, job_id, **kwargs):
        return SimpleNamespace(
            provider_id='CN_VIDEO',
            model_id='video-model-v1',
            deployment_region='MAINLAND_CHINA',
            endpoint_profile_id='cn-video-endpoint',
            endpoint_class='CN_VIDEO_PUBLIC',
            reference_count=2,
            shot_count=1,
            max_paid_attempts=1,
            estimated_provider_requests=1,
            target_episode_duration_seconds=120,
            native_generation_resolution='1920x1080',
            native_generation_fps=24,
            delivery_resolution='3840x2160',
            target_fps=30,
            delivery_strategy='DETERMINISTIC_UPSCALE',
            quality_mode='HIGH',
            transmitted_content_types=('TEXT', 'REFERENCE_IMAGE'),
            authorization_fingerprint='a' * 64,
        )
    def run_job(self, *args, **kwargs):
        pass
    def prepare_generation_briefs(self, project_id, job_id):
        return (SimpleNamespace(
            id='brief-1', shot_id='shot_001', origin='AI_COMPILED',
            sha256='b' * 64, parent_brief_id=None,
            character_context=({'id': 'char_001', 'name': '女主'},),
            action='走进房间', framing='MEDIUM', composition='三分法',
            camera_movement='STATIC', lens_intent='NORMAL',
            lighting={'quality': 'soft'}, mood='克制',
            continuity_constraints=('服装一致',), negative_constraints=(),
            dialogue_audio_intent='', target_duration_seconds=4.0,
        ),)
    def save_generation_brief_override(self, *args, **kwargs):
        pass

page._render_primary_action(
    Orchestrator(), None, project, job, {'ready': True}, {'completed_shots': 0}
)
"""
    ).run()

    assert not app.exception
    metrics = {item.label: str(item.value) for item in app.metric}
    assert metrics["视频 Provider"] == "CN_VIDEO"
    assert metrics["模型"] == "video-model-v1"
    assert metrics["区域 / 部署类型"] == "MAINLAND_CHINA / CN_VIDEO_PUBLIC"
    assert metrics["参考图数量"] == "2"
    assert metrics["目标成片时长"] == "120s"
    assert metrics["Provider 原生生成"] == "1920x1080 · 24fps"
    assert metrics["最终交付"] == "3840x2160 · 30fps"
    assert any(item.label == "生成简报 · 付费前可编辑" for item in app.expander)
    assert any(item.label == "人物 / 动作" for item in app.text_area)
    info = "\n".join(str(item.value) for item in app.info)
    assert "不是原生 4K" in info
    warning = "\n".join(str(item.value) for item in app.warning)
    assert "CN_VIDEO" in warning
    assert "MAINLAND_CHINA" in warning
    assert "文本" in warning and "参考图片" in warning
    assert app.checkbox[0].key.endswith("a" * 64)
    assert "区域与传输内容披露" in app.checkbox[0].label


def test_failure_reason_does_not_leak_traceback():
    reason = page._safe_failure_reason("runtime error\nTraceback (most recent call last):\nsecret absolute path")
    assert "Traceback" not in reason
    assert "secret absolute path" not in reason


def test_relative_path_rejects_absolute_and_traversal_paths():
    assert page._relative_path(r"C:\private\secret.mp4") == "[project-relative path unavailable]"
    assert page._relative_path("../private/secret.mp4") == "[project-relative path unavailable]"
    assert page._relative_path("production/job-1/shot.mp4") == "production/job-1/shot.mp4"
