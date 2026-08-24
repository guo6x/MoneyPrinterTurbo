from __future__ import annotations

import pytest

from aidrama_studio.services import DeterministicMockVisionProvider, UnavailableVisionProvider, VisionQCService
from test.aidrama_studio.test_production_qc import _artifact_context
from test.aidrama_studio.test_production_execution import context as _execution_context


@pytest.fixture
def context(tmp_path):
    return _execution_context.__wrapped__(tmp_path)


def test_unconfigured_vision_qc_is_truthfully_not_run(context):
    repository, project, execution, artifact = _artifact_context(context)
    result = VisionQCService(repository, provider=UnavailableVisionProvider()).analyze(project.id, execution.id, artifact.id)
    assert result.status == "NOT_RUN"
    assert result.analysis_kind == "AI_ANALYSIS"


def test_mock_vision_qc_is_structured_and_project_scoped(context):
    repository, project, execution, artifact = _artifact_context(context)
    result = VisionQCService(repository, provider=DeterministicMockVisionProvider({"CHARACTER_CONSISTENCY": {"score": 1.0}})).analyze(project.id, execution.id, artifact.id)
    assert result.status == "AI_ANALYSIS"
    assert result.metrics["CHARACTER_CONSISTENCY"]["score"] == 1.0
    with pytest.raises(Exception):
        VisionQCService(repository).analyze("other-project", execution.id, artifact.id)
