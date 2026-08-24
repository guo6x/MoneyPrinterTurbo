"""MoneyPrinterTurbo runtime adapter implementation.

The adapter owns the only translation between AIDrama's frozen production
snapshot and an MPT runtime client.  The client is injected deliberately: the
existing MPT ``task.start`` pipeline is a complete episode generator and must
not be invoked implicitly by this boundary phase.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .production_adapter import ProductionRuntimeAdapter, RuntimeSubmission


class MPTAdapterError(ValueError):
    """Raised for invalid MPT adapter input or an unmappable runtime result."""


class MPTInputMapper:
    """Pure mapper from a ProductionInputSnapshot to an MPT-shaped payload."""

    @staticmethod
    def map_snapshot(snapshot: Any) -> dict[str, object]:
        required = (
            "project_id",
            "story_revision_id",
            "script_revision_id",
            "shot_plan_revision_id",
        )
        if snapshot is None or any(not isinstance(getattr(snapshot, key, None), str) or not getattr(snapshot, key).strip() for key in required):
            raise MPTAdapterError("snapshot 缺少 project/revision identity")

        references = getattr(snapshot, "reference_asset_versions", None)
        shots = getattr(snapshot, "shot_parameters", None)
        if not isinstance(references, Mapping) or not isinstance(shots, Mapping):
            raise MPTAdapterError("snapshot references 和 shot parameters 必须是 mapping")

        mapped_shots: list[dict[str, object]] = []
        for shot_id, raw_parameters in shots.items():
            if not isinstance(shot_id, str) or not shot_id.strip() or not isinstance(raw_parameters, Mapping):
                raise MPTAdapterError("snapshot 包含无效 shot identity 或 parameters")
            parameters = MPTInputMapper._plain_mapping(raw_parameters)
            prompt = MPTInputMapper._prompt(parameters)
            mapped_shots.append(
                {
                    "shot_id": shot_id,
                    "prompt": prompt,
                    "parameters": parameters,
                }
            )

        return {
            "project_id": snapshot.project_id,
            "story_revision_id": snapshot.story_revision_id,
            "script_revision_id": snapshot.script_revision_id,
            "shot_plan_revision_id": snapshot.shot_plan_revision_id,
            "reference_asset_versions": MPTInputMapper._plain_mapping(references),
            "shots": mapped_shots,
        }

    map_input = map_snapshot

    @staticmethod
    def _plain_mapping(value: Mapping[str, object]) -> dict[str, object]:
        def thaw(item: object) -> object:
            if isinstance(item, Mapping):
                return {str(key): thaw(child) for key, child in item.items()}
            if isinstance(item, (tuple, list)):
                return [thaw(child) for child in item]
            if isinstance(item, (set, frozenset)):
                return [thaw(child) for child in item]
            return item

        return {str(key): thaw(item) for key, item in value.items()}

    @staticmethod
    def _prompt(parameters: Mapping[str, object]) -> str:
        for key in ("prompt", "visual_intent", "action", "dialogue_or_narration"):
            value = parameters.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""


class MPTProductionAdapter(ProductionRuntimeAdapter):
    """Adapter for an explicitly injected MPT runtime client."""

    name = "mpt"

    STATUS_MAP = {
        "waiting": "QUEUED",
        "queued": "QUEUED",
        "processing": "RUNNING",
        "running": "RUNNING",
        "completed": "SUCCEEDED",
        "complete": "SUCCEEDED",
        "succeeded": "SUCCEEDED",
        "failed": "FAILED",
        "error": "FAILED",
        "cancelled": "CANCELLED",
        "canceled": "CANCELLED",
    }

    def __init__(self, runtime: Any | None = None, *, mapper: MPTInputMapper | None = None):
        self._runtime = runtime
        self.mapper = mapper or MPTInputMapper()

    def map_input(self, snapshot: Any) -> dict[str, object]:
        return self.mapper.map_snapshot(snapshot)

    def validate(self, snapshot: Any) -> bool:
        try:
            mapped = self.map_input(snapshot)
        except (MPTAdapterError, TypeError, ValueError):
            return False
        return self._validate_mapped(mapped)

    def _validate_mapped(self, mapped: Mapping[str, object]) -> bool:
        runtime_validate = getattr(self._runtime, "validate", None)
        if callable(runtime_validate):
            result = runtime_validate(mapped)
            return result is not False
        return bool(mapped["project_id"] and mapped["shots"])

    def submit(self, snapshot: Any) -> RuntimeSubmission:
        mapped = self.map_input(snapshot)
        if not self._validate_mapped(mapped):
            raise MPTAdapterError("MPT runtime input validation failed")
        if self._runtime is None:
            raise NotImplementedError("MPT runtime client is not configured")
        result = self._runtime.submit(mapped)
        return self._submission(result)

    def cancel(self, runtime_reference: str) -> bool:
        if self._runtime is None:
            raise NotImplementedError("MPT runtime client is not configured")
        result = self._runtime.cancel(runtime_reference)
        return result is not False

    def get_status(self, runtime_reference: str) -> str:
        if self._runtime is None:
            raise NotImplementedError("MPT runtime client is not configured")
        raw = self._runtime.get_status(runtime_reference)
        return self.map_status(raw)

    @classmethod
    def map_status(cls, raw: Any) -> str:
        if isinstance(raw, Mapping):
            raw = raw.get("status") or raw.get("state")
        if hasattr(raw, "status"):
            raw = raw.status
        if hasattr(raw, "value"):
            raw = raw.value
        key = str(raw or "").strip().lower()
        try:
            return cls.STATUS_MAP[key]
        except KeyError as exc:
            raise MPTAdapterError(f"unknown MPT runtime status: {raw}") from exc

    @staticmethod
    def _submission(result: Any) -> RuntimeSubmission:
        if isinstance(result, RuntimeSubmission):
            return result
        if isinstance(result, Mapping):
            reference = result.get("runtime_reference") or result.get("reference") or result.get("task_id") or result.get("id")
            metadata = result.get("metadata") or {}
            if reference:
                return RuntimeSubmission(runtime_reference=str(reference), metadata=dict(metadata) if isinstance(metadata, Mapping) else {})
        reference = getattr(result, "runtime_reference", None) or getattr(result, "reference", None) or getattr(result, "task_id", None) or getattr(result, "id", None)
        if reference:
            return RuntimeSubmission(runtime_reference=str(reference))
        if isinstance(result, str) and result.strip():
            return RuntimeSubmission(runtime_reference=result.strip())
        raise MPTAdapterError("MPT runtime submit 未返回 runtime reference")
