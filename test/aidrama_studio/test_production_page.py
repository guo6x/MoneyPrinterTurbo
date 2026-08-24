from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from aidrama_studio.pages import production


def test_production_page_exposes_required_execution_sections():
    source = Path(production.__file__).read_text(encoding="utf-8")
    for label in (
        "Production Jobs",
        "Production Readiness Check",
        "Execution",
        "Event timeline",
        "Artifact metadata",
        "Submit Execution",
    ):
        assert label in source
    assert "生成视频" not in source


def test_blocked_readiness_is_rendered_without_job_creation():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages import production as page

project = SimpleNamespace(id="project-1", title="Blocked project")
class FakeProduction:
    def validate_job_readiness(self, project_id, revision_id=None):
        return {"ready": False, "blocked_reasons": ["approved Shot Plan is required"], "shot_count": 0}
    def list_jobs(self, project_id):
        return []
class FakeExecution:
    def __init__(self, **kwargs):
        pass
page.current_project_or_stop = lambda: project
page.ProductionService = FakeProduction
page.ProductionExecutionService = FakeExecution
page.render()
"""
    ).run(timeout=30)
    assert not app.exception
    assert any("Production 尚未就绪" in warning.value for warning in app.warning)
    assert any(button.disabled for button in app.button if button.label == "Create Production Job")


def test_job_creation_and_execution_submission_use_services_only(monkeypatch):
    monkeypatch.setattr(production.st, "rerun", lambda: None)
    project = SimpleNamespace(id="project-1")
    job = SimpleNamespace(id="job-1")

    class FakeProduction:
        def __init__(self):
            self.calls = []
        def create_production_job(self, project_id):
            self.calls.append(("create", project_id))
            return job

    class FakeExecution:
        def __init__(self):
            self.calls = []
        def enqueue_job(self, project_id, job_id):
            self.calls.append(("enqueue", project_id, job_id))

    production_service = FakeProduction()
    execution_service = FakeExecution()
    production._create_job(production_service, project)
    production._queue_execution(execution_service, project, job)
    assert production_service.calls == [("create", project.id)]
    assert execution_service.calls == [("enqueue", project.id, job.id)]


def test_execution_detail_renders_status_events_and_artifact_metadata():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages import production as page

project = SimpleNamespace(id="project-1", title="Execution project")
job = SimpleNamespace(id="job-1", status="SUCCEEDED", created_at="now", shot_plan_revision_id="plan-1")
execution = SimpleNamespace(id="execution-1", status="SUCCEEDED", worker_type="mock", created_at="now", started_at="start", finished_at="finish")
event = SimpleNamespace(event_type="PROGRESS", payload_json={"progress": 75}, created_at="event-time")
artifact = SimpleNamespace(artifact_type="manifest", path="artifacts/manifest.json", metadata_json={"runtime": "mock"}, created_at="artifact-time")

class FakeProduction:
    def validate_job_readiness(self, project_id, revision_id=None):
        return {"ready": True, "blocked_reasons": [], "shot_count": 2}
    def list_jobs(self, project_id):
        return [job]
class FakeExecution:
    def __init__(self, **kwargs):
        pass
    def list_executions(self, project_id, job_id):
        return [execution]
    def list_events(self, project_id, execution_id):
        return [event]
    def list_artifacts(self, project_id, execution_id):
        return [artifact]
page.current_project_or_stop = lambda: project
page.ProductionService = FakeProduction
page.ProductionExecutionService = FakeExecution
page.render()
"""
    ).run(timeout=30)
    assert not app.exception
    assert any("Execution · SUCCEEDED" in item.value for item in app.markdown)
    assert any("Progress update" in item.value for item in app.markdown)
    assert any("Artifact metadata" in item.value for item in app.markdown)
    assert any("Runtime · mock" in item.value for item in app.caption)


def test_event_label_mapping():
    event = SimpleNamespace(event_type="SHOT_COMPLETED")
    assert production._event_label(event) == "Shot completed"
