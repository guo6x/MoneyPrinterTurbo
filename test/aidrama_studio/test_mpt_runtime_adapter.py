from __future__ import annotations

import pytest

from aidrama_studio.domain import ProductionInputSnapshot
from aidrama_studio.services.adapters import MPTAdapterError, MPTInputMapper, MPTProductionAdapter


def _snapshot() -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        project_id="project-1",
        story_revision_id="story-1",
        script_revision_id="script-1",
        shot_plan_revision_id="plan-1",
        reference_asset_versions={"CHARACTER:hero": "asset-version-1"},
        shot_parameters={
            "shot-1": {
                "prompt": "Hero opens the door",
                "duration_seconds": 3,
                "camera_movement": "STATIC",
            }
        },
    )


def test_mpt_input_mapping_preserves_identity_shot_prompt_references_and_parameters():
    mapped = MPTInputMapper.map_snapshot(_snapshot())
    assert mapped["project_id"] == "project-1"
    assert mapped["story_revision_id"] == "story-1"
    assert mapped["script_revision_id"] == "script-1"
    assert mapped["shot_plan_revision_id"] == "plan-1"
    assert mapped["reference_asset_versions"] == {"CHARACTER:hero": "asset-version-1"}
    assert mapped["shots"] == [{
        "shot_id": "shot-1",
        "prompt": "Hero opens the door",
        "parameters": {"prompt": "Hero opens the door", "duration_seconds": 3, "camera_movement": "STATIC"},
    }]


def test_mpt_adapter_validation_rejects_incomplete_snapshot():
    adapter = MPTProductionAdapter()
    assert adapter.validate(_snapshot()) is True
    invalid = _snapshot().model_copy(update={"project_id": ""})
    assert adapter.validate(invalid) is False


class FakeMPTRuntime:
    def __init__(self):
        self.payload = None
        self.cancelled = None
        self.status = "waiting"

    def validate(self, payload):
        self.payload = payload
        return True

    def submit(self, payload):
        self.payload = payload
        self.status = "processing"
        return {"task_id": "mpt-task-1", "metadata": {"engine": "fake-mpt"}}

    def cancel(self, reference):
        self.cancelled = reference
        self.status = "failed"
        return True

    def get_status(self, reference):
        return self.status


def test_mpt_submit_cancel_and_status_mapping_lifecycle():
    runtime = FakeMPTRuntime()
    adapter = MPTProductionAdapter(runtime)
    submission = adapter.submit(_snapshot())
    assert submission.runtime_reference == "mpt-task-1"
    assert runtime.payload["shots"][0]["shot_id"] == "shot-1"
    assert adapter.get_status(submission.runtime_reference) == "RUNNING"
    assert adapter.cancel(submission.runtime_reference) is True
    assert runtime.cancelled == "mpt-task-1"
    assert adapter.get_status(submission.runtime_reference) == "FAILED"
    for raw, expected in (("waiting", "QUEUED"), ("processing", "RUNNING"), ("completed", "SUCCEEDED"), ("failed", "FAILED")):
        runtime.status = raw
        assert adapter.get_status(submission.runtime_reference) == expected


def test_mpt_status_and_submission_failures_are_explicit():
    class BadRuntime:
        def submit(self, payload):
            return {}

        def get_status(self, reference):
            return "mystery"

    adapter = MPTProductionAdapter(BadRuntime())
    with pytest.raises(MPTAdapterError, match="runtime reference"):
        adapter.submit(_snapshot())
    with pytest.raises(MPTAdapterError, match="unknown"):
        adapter.get_status("mpt-task")
