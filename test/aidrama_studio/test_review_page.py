from types import SimpleNamespace

from aidrama_studio.pages.review import _result_shot_id, _review_targets


def test_result_shot_id_uses_execution_snapshot_when_qc_has_no_direct_shot() -> None:
    result = SimpleNamespace(execution_id="execution-1")
    execution = SimpleNamespace(
        input_snapshot=SimpleNamespace(shot_parameters={"shot_001": {}})
    )

    assert _result_shot_id(result, execution) == "shot_001"


def test_result_shot_id_prefers_explicit_qc_projection() -> None:
    result = SimpleNamespace(shot_id="shot_explicit")
    execution = SimpleNamespace(
        input_snapshot=SimpleNamespace(shot_parameters={"shot_snapshot": {}})
    )

    assert _result_shot_id(result, execution) == "shot_explicit"


def test_review_targets_include_every_execution_in_shot_order() -> None:
    project = SimpleNamespace(id="project-1")
    executions = [
        SimpleNamespace(
            id="execution-1",
            input_snapshot=SimpleNamespace(shot_parameters={"shot_001": {}}),
        ),
        SimpleNamespace(
            id="execution-2",
            input_snapshot=SimpleNamespace(shot_parameters={"shot_002": {}}),
        ),
    ]
    state = SimpleNamespace(
        shots=(
            SimpleNamespace(id="production-shot-1", shot_id="shot_001", order_index=1),
            SimpleNamespace(id="production-shot-2", shot_id="shot_002", order_index=2),
        )
    )

    class FakeQC:
        def list_results(self, project_id: str, execution_id: str):
            assert project_id == project.id
            return [SimpleNamespace(id=f"qc-{execution_id}", execution_id=execution_id)]

    targets = _review_targets(project, executions, FakeQC(), state)

    assert [target["execution"].id for target in targets] == ["execution-1", "execution-2"]
    assert [target["shot"].id for target in targets] == ["production-shot-1", "production-shot-2"]
    assert [target["shot_number"] for target in targets] == [1, 2]
