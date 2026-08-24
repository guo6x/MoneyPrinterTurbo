from __future__ import annotations

from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from aidrama_studio.pages import production


def _base_page_script(qc_class: str, qc_body: str) -> str:
    return f"""
from types import SimpleNamespace
from aidrama_studio.pages import production as page

project = SimpleNamespace(id="project-1", title="QC project")
job = SimpleNamespace(id="job-1", status="SUCCEEDED", created_at="now", shot_plan_revision_id="plan-1")
execution = SimpleNamespace(id="execution-1", status="SUCCEEDED", worker_type="mpt", created_at="now", started_at="start", finished_at="finish")
artifact = SimpleNamespace(id="artifact-1", execution_id="execution-1", artifact_type="video", path="production/execution-1/shot.mp4", metadata_json={{"execution_id": "execution-1", "shot_id": "shot-1", "reference_versions": {{"CHARACTER:hero": "version-1"}}, "runtime": "mpt"}}, created_at="artifact-time")
event = SimpleNamespace(event_type="FINISHED", payload_json={{}}, created_at="event-time")

class FakeProduction:
    def validate_job_readiness(self, project_id, revision_id=None):
        return {{"ready": True, "blocked_reasons": [], "shot_count": 1}}
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

class {qc_class}:
    def __init__(self, **kwargs):
        self.calls = []
    {qc_body}

page.current_project_or_stop = lambda: project
page.ProductionService = FakeProduction
page.ProductionExecutionService = FakeExecution
page.ProductionQCService = {qc_class}
page.render()
"""


def test_qc_section_and_actions_are_exposed_in_production_source():
    source = open(production.__file__, encoding="utf-8").read()
    for label in ("Quality Control", "Run QC", "Retry QC", "QC History", "Submit Review", "Traceability"):
        assert label in source


def test_no_qc_result_state_renders():
    app = AppTest.from_string(
        _base_page_script(
            "FakeQC",
            """
    def list_results(self, project_id, execution_id=None):
        return []
    def list_metrics(self, project_id, result_id):
        return []
    def list_reviews(self, project_id, result_id=None):
        return []
""",
        )
    ).run(timeout=30)
    assert not app.exception
    assert any("No QC result yet" in item.value for item in app.info)


def test_qc_pass_renders_checks_and_traceability():
    app = AppTest.from_string(
        _base_page_script(
            "FakeQC",
            """
    def list_results(self, project_id, execution_id=None):
        return [SimpleNamespace(id="qc-1", artifact_id="artifact-1", status="QC_PASS", summary_json={"passed": 3, "failed": 0, "skipped": 0}, report_path="production/execution-1/qc/qc_report.json")]
    def list_metrics(self, project_id, result_id):
        return [SimpleNamespace(metric_name="audio_stream", status="PASS", message="audio stream 存在"), SimpleNamespace(metric_name="traceability", status="PASS", message="traceability 有效")]
    def list_reviews(self, project_id, result_id=None):
        return []
""",
        )
    ).run(timeout=30)
    assert not app.exception
    assert any("QC Run #1 · QC_PASS" in item.value for item in app.markdown)
    assert any("audio stream" in item.value for item in app.markdown)
    assert any("Reference versions" in item.value for item in app.caption)
    assert any("qc_report.json" in item.value for item in app.caption)


def test_qc_failed_renders_failed_metrics():
    app = AppTest.from_string(
        _base_page_script(
            "FakeQC",
            """
    def list_results(self, project_id, execution_id=None):
        return [SimpleNamespace(id="qc-1", artifact_id="artifact-1", status="QC_FAILED", summary_json={"passed": 2, "failed": 1, "skipped": 0}, report_path="production/execution-1/qc/qc_report.json")]
    def list_metrics(self, project_id, result_id):
        return [SimpleNamespace(metric_name="audio_stream", status="FAIL", message="audio stream 缺失")]
    def list_reviews(self, project_id, result_id=None):
        return []
""",
        )
    ).run(timeout=30)
    assert not app.exception
    assert any("QC Run #1 · QC_FAILED" in item.value for item in app.markdown)
    assert any("audio stream 缺失" in item.value for item in app.markdown)
    assert any("QC failed" in item.value for item in app.error)


def test_history_and_review_render():
    app = AppTest.from_string(
        _base_page_script(
            "FakeQC",
            """
    def list_results(self, project_id, execution_id=None):
        return [SimpleNamespace(id="qc-1", artifact_id="artifact-1", status="QC_FAILED", summary_json={"passed": 0, "failed": 1, "skipped": 0}, report_path="production/execution-1/qc/qc_report.json"), SimpleNamespace(id="qc-2", artifact_id="artifact-1", status="QC_PASS", summary_json={"passed": 1, "failed": 0, "skipped": 0}, report_path="production/execution-1/qc/qc_report-qc-2.json")]
    def list_metrics(self, project_id, result_id):
        return [SimpleNamespace(metric_name="artifact_exists", status="PASS", message="artifact 文件存在")]
    def list_reviews(self, project_id, result_id=None):
        if result_id == "qc-1":
            return [SimpleNamespace(decision="REJECTED", reviewer="qa", notes="audio issue")]
        return []
""",
        )
    ).run(timeout=30)
    assert not app.exception
    assert any("QC Run #1" in item.value for item in app.markdown)
    assert any("QC Run #2" in item.value for item in app.markdown)
    assert any("Human Review · REJECTED" in item.value for item in app.caption)


def test_run_and_retry_actions_invoke_canonical_service(monkeypatch):
    monkeypatch.setattr(production.st, "rerun", lambda: None)
    project = SimpleNamespace(id="project-1")
    execution = SimpleNamespace(id="execution-1", status="SUCCEEDED")
    artifact = SimpleNamespace(id="artifact-1")

    class FakeQC:
        def __init__(self):
            self.calls = []
        def run_qc(self, project_id, execution_id, artifact_id):
            self.calls.append(("run", project_id, execution_id, artifact_id))
        def retry_qc(self, project_id, execution_id, artifact_id):
            self.calls.append(("retry", project_id, execution_id, artifact_id))

    service = FakeQC()
    production._run_qc(service, project, execution, artifact)
    production._run_qc(service, project, execution, artifact, retry=True)
    assert service.calls == [("run", "project-1", "execution-1", "artifact-1"), ("retry", "project-1", "execution-1", "artifact-1")]


def test_human_review_submission_uses_domain_decision_without_mutating_metrics(monkeypatch):
    monkeypatch.setattr(production.st, "rerun", lambda: None)
    project = SimpleNamespace(id="project-1")
    result = SimpleNamespace(id="qc-1")

    class FakeQC:
        def __init__(self):
            self.calls = []
        def create_review(self, project_id, result_id, decision, notes=""):
            self.calls.append((project_id, result_id, decision.value, notes))

    service = FakeQC()
    production._submit_review(service, project, result, "APPROVED", "looks good")
    assert service.calls == [("project-1", "qc-1", "APPROVED", "looks good")]


def test_project_isolation_and_absolute_path_are_not_leaked():
    assert production._relative_path(r"C:\\private\\secret.mp4") == "[project-relative path unavailable]"
    assert production._relative_path("../private/secret.mp4") == "[project-relative path unavailable]"
    assert "C:" not in production._relative_path(r"C:\\private\\secret.mp4")


def test_qc_queries_are_scoped_to_selected_project_and_execution():
    app = AppTest.from_string(
        _base_page_script(
            "FakeQC",
            """
    def list_results(self, project_id, execution_id=None):
        assert project_id == "project-1"
        assert execution_id == "execution-1"
        return []
    def list_metrics(self, project_id, result_id):
        assert project_id == "project-1"
        return []
    def list_reviews(self, project_id, result_id=None):
        assert project_id == "project-1"
        return []
""",
        )
    ).run(timeout=30)
    assert not app.exception


def test_qc_trigger_rejects_incomplete_execution(monkeypatch):
    warnings = []
    monkeypatch.setattr(production.st, "warning", warnings.append)
    project = SimpleNamespace(id="project-1")
    execution = SimpleNamespace(id="execution-1", status="RUNNING")
    artifact = SimpleNamespace(id="artifact-1")

    class FakeQC:
        def run_qc(self, *args):
            raise AssertionError("incomplete execution must not invoke QC")

    production._run_qc(FakeQC(), project, execution, artifact)
    assert warnings and "not complete" in warnings[0]
