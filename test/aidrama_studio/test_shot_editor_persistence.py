from __future__ import annotations

import pytest

from aidrama_studio.services import CreativeLockService, ShotService, ShotServiceError
from test.aidrama_studio.test_production_execution import context as _execution_context


def _draft(repository, project):
    service = ShotService(repository)
    approved = repository.get_shot_revision("shot_001")
    return service, service.save_draft(
        project.id, approved["content"].model_copy(deep=True), revision_id=approved["id"]
    )


def test_every_visible_shot_editor_field_round_trips(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    service, draft = _draft(repository, project)

    service.update_shot_fields(
        project.id,
        draft["id"],
        "shot_001",
        {
            "scene_id": "scene_001",
            "shot_size": "CLOSE_UP",
            "camera_angle": "LOW_ANGLE",
            "camera_movement": "PUSH_IN",
            "lens": "PORTRAIT",
            "composition": "rule of thirds",
            "duration_seconds": 8.0,
            "risk_level": "HIGH",
            "risk_reasons": "EMOTIONAL_CLOSEUP, CAMERA_MOTION",
            "risk_override": True,
            "risk_override_note": "人工确认可控",
            "subject": "char_001",
            "action": "推开门",
            "expression": "坚定",
            "eyeline": "camera",
            "lighting": "hard rim light",
            "blocking": "从左向右移动",
            "dialogue_or_narration": "我回来了。",
            "visual_intent": "克制的重逢",
            "transition_hint": "match cut",
            "status": "LOCKED",
        },
    )

    shot = repository.get_shot_revision(draft["id"])["content"].shots[0]
    assert shot.shot_size.value == "CLOSE_UP"
    assert shot.camera_angle.value == "LOW_ANGLE"
    assert shot.camera_movement.value == "PUSH_IN"
    assert shot.lens.value == "PORTRAIT"
    assert shot.duration_seconds == 8.0
    assert shot.risk_level.value == "HIGH"
    assert shot.risk_override is True
    assert shot.subject == ["char_001"]
    assert shot.lighting.quality == "hard rim light"
    assert shot.blocking.movement == "从左向右移动"
    assert shot.dialogue_or_narration == "我回来了。"
    assert shot.status.value == "LOCKED"
    active_locks = CreativeLockService(repository).active(
        project.id, entity_kind="SHOT", stable_entity_id="shot_001"
    )
    assert len(active_locks) == 1
    assert active_locks[0].field_path == "*"

    proposal = service.recommend_duration_rebalance(draft["id"], 2.0)
    assert proposal["feasible"] is False
    assert proposal["locked_total"] == 8.0
    assert proposal["suggestions"] == []

    service.update_shot_fields(
        project.id, draft["id"], "shot_001", {"status": "PLANNED"}
    )
    assert CreativeLockService(repository).active(
        project.id, entity_kind="SHOT", stable_entity_id="shot_001"
    ) == ()


def test_invalid_shot_edit_fails_without_silent_fallback(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    service, draft = _draft(repository, project)
    before = repository.get_shot_revision(draft["id"])["content"]

    with pytest.raises(ShotServiceError, match="Shot 编辑无效"):
        service.update_shot_fields(
            project.id, draft["id"], "shot_001", {"shot_size": "NOT_A_SIZE"}
        )
    after = repository.get_shot_revision(draft["id"])["content"]
    assert after == before

    invalid = before.model_dump(mode="json")
    invalid["shots"] = []
    with pytest.raises(ShotServiceError, match="Draft 无效"):
        service.save_draft(project.id, invalid, revision_id=draft["id"])
    assert repository.get_shot_revision(draft["id"])["content"] == before


class _SelectiveGateway:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def readiness(self, _project_id):
        return {"ready": True}

    def generate_validated_json(self, _project_id, _prompt, *, validator, **_kwargs):
        self.calls += 1
        return validator(self.payload)


def test_selective_shot_regeneration_creates_new_draft_and_preserves_locked_non_target(tmp_path):
    repository, project = _execution_context.__wrapped__(tmp_path)
    service, draft = _draft(repository, project)
    service.update_shot_fields(
        project.id, draft["id"], "shot_001", {"status": "LOCKED", "duration_seconds": 8.0}
    )
    service.add_shot(draft["id"])
    source = repository.get_shot_revision(draft["id"])
    candidate = source["content"].shots[1].model_dump(mode="json")
    candidate["action"] = "只重新生成第二个镜头"
    candidate["visual_intent"] = "新的第二镜头候选"
    gateway = _SelectiveGateway(candidate)
    selective = ShotService(repository, llm_gateway=gateway)

    result = selective.regenerate_shot(project, draft["id"], "shot_002")

    assert result["id"] != draft["id"]
    assert result["status"].value == "DRAFT"
    assert result["generation_input"] == {
        "operation": "SHOT_SELECTIVE_REGENERATION",
        "parent_revision_id": draft["id"],
        "target_shot_id": "shot_002",
    }
    assert result["content"].shots[0] == source["content"].shots[0]
    assert result["content"].shots[1].action == "只重新生成第二个镜头"
    assert repository.get_shot_revision(draft["id"])["content"] == source["content"]
    assert repository.get_shot_revision("shot_001")["status"].value == "APPROVED"
    assert gateway.calls == 1

    with pytest.raises(ShotServiceError, match="锁定镜头"):
        selective.regenerate_shot(project, draft["id"], "shot_001")
    assert gateway.calls == 1
