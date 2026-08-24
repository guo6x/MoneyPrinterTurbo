"""Deterministic in-process runtime adapter for boundary tests."""

from __future__ import annotations

from collections import defaultdict, deque
from uuid import uuid4

from .production_adapter import ProductionRuntimeAdapter, RuntimeEvent, RuntimeSubmission


class MockProductionAdapter(ProductionRuntimeAdapter):
    name = "mock"

    def __init__(self, *, reject_validation: bool = False, fail_submit: bool = False):
        self.reject_validation = reject_validation
        self.fail_submit = fail_submit
        self._statuses: dict[str, str] = {}
        self._snapshots: dict[str, object] = {}
        self._events: dict[str, deque[RuntimeEvent]] = defaultdict(deque)

    @property
    def submitted_snapshots(self) -> dict[str, object]:
        return dict(self._snapshots)

    def validate(self, snapshot) -> bool:
        if self.reject_validation:
            return False
        if snapshot is None or not getattr(snapshot, "project_id", ""):
            return False
        return True

    def submit(self, snapshot) -> RuntimeSubmission:
        if not self.validate(snapshot):
            raise ValueError("mock runtime rejected snapshot")
        if self.fail_submit:
            raise RuntimeError("mock runtime submit failed")
        reference = f"mock-{uuid4().hex}"
        self._statuses[reference] = "RUNNING"
        self._snapshots[reference] = snapshot
        return RuntimeSubmission(runtime_reference=reference)

    def cancel(self, runtime_reference: str) -> bool:
        self._require_reference(runtime_reference)
        if self._statuses[runtime_reference] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            self._statuses[runtime_reference] = "CANCELLED"
            self._events[runtime_reference].append(RuntimeEvent("CANCELLED", {"reason": "cancelled"}))
        return True

    def get_status(self, runtime_reference: str) -> str:
        self._require_reference(runtime_reference)
        return self._statuses[runtime_reference]

    def progress(self, runtime_reference: str, progress: int | float, **payload: object) -> None:
        self._require_running(runtime_reference)
        data = dict(payload)
        data["progress"] = progress
        self._events[runtime_reference].append(RuntimeEvent("PROGRESS", data))

    emit_progress = progress

    def shot_completed(self, runtime_reference: str, shot_id: str, **payload: object) -> None:
        self._require_running(runtime_reference)
        data = dict(payload)
        data["shot_id"] = shot_id
        self._events[runtime_reference].append(RuntimeEvent("SHOT_COMPLETED", data))

    def succeed(self, runtime_reference: str, *, artifacts: list[dict[str, object]] | None = None, **payload: object) -> None:
        self._require_running(runtime_reference)
        self._statuses[runtime_reference] = "SUCCEEDED"
        data = dict(payload)
        if artifacts is not None:
            data["artifacts"] = artifacts
        self._events[runtime_reference].append(RuntimeEvent("FINISHED", data))

    success = succeed

    def fail(self, runtime_reference: str, error: str = "mock failure", **payload: object) -> None:
        self._require_running(runtime_reference)
        self._statuses[runtime_reference] = "FAILED"
        data = dict(payload)
        data["error"] = error
        self._events[runtime_reference].append(RuntimeEvent("FAILED", data))

    def drain_events(self, runtime_reference: str) -> list[RuntimeEvent]:
        self._require_reference(runtime_reference)
        events = list(self._events[runtime_reference])
        self._events[runtime_reference].clear()
        return events

    def _require_reference(self, runtime_reference: str) -> None:
        if runtime_reference not in self._statuses:
            raise KeyError(f"unknown mock runtime reference: {runtime_reference}")

    def _require_running(self, runtime_reference: str) -> None:
        self._require_reference(runtime_reference)
        if self._statuses[runtime_reference] != "RUNNING":
            raise RuntimeError("mock runtime is not running")
