from types import SimpleNamespace

from aidrama_studio.pages.review import _result_shot_id


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
