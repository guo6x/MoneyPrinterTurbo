"""Seedance video adapter boundary.

This module deliberately contains only the provider HTTP seam and explicit
input/status mapping. It never writes SQLite or project files; the worker and
artifact storage own those responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from aidrama_studio.domain import ProductionInputSnapshot

from .production_adapter import ProductionRuntimeAdapter, RuntimeSubmission


class SeedanceAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeedanceProviderConfig:
    api_key: str = ""
    base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    model: str = "seedance-1-0-pro"
    allow_paid_live_tests: bool = False
    timeout_seconds: float = 30.0

    @classmethod
    def from_environment(cls, **overrides: object) -> "SeedanceProviderConfig":
        values: dict[str, object] = {
            "api_key": os.environ.get("ARK_API_KEY", "").strip(),
            "base_url": os.environ.get("SEEDANCE_BASE_URL", cls.base_url).strip(),
            "model": os.environ.get("SEEDANCE_VIDEO_MODEL", cls.model).strip(),
            "allow_paid_live_tests": os.environ.get("AIDRAMA_ALLOW_PAID_LIVE_TESTS", "") == "1",
        }
        values.update(overrides)
        return cls(**values)

    def validate(self, *, require_live: bool = False) -> None:
        parsed = urlsplit(self.base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SeedanceAdapterError("Seedance base_url 必须是 HTTP(S) 地址")
        if not self.model.strip():
            raise SeedanceAdapterError("Seedance model 不能为空")
        if self.timeout_seconds <= 0:
            raise SeedanceAdapterError("Seedance timeout 必须为正数")
        if require_live and (not self.api_key or not self.allow_paid_live_tests):
            raise SeedanceAdapterError("Seedance live request 需要 key 与显式付费授权")


class SeedanceInputMapper:
    @staticmethod
    def map_snapshot(snapshot: ProductionInputSnapshot) -> dict[str, object]:
        if not isinstance(snapshot, ProductionInputSnapshot):
            raise SeedanceAdapterError("Seedance 需要 ProductionInputSnapshot")
        if not snapshot.project_id or not snapshot.shot_parameters:
            raise SeedanceAdapterError("snapshot 缺少 project 或 shot")
        shots: list[dict[str, object]] = []
        for shot_id, parameters in snapshot.shot_parameters.items():
            if not isinstance(parameters, Mapping):
                raise SeedanceAdapterError("shot parameters 必须是 mapping")
            params = SeedanceInputMapper._plain(parameters)
            shots.append({"shot_id": shot_id, "prompt": str(params.get("prompt") or params.get("visual_intent") or params.get("action") or ""), "parameters": params})
        return {
            "project_id": snapshot.project_id,
            "story_revision_id": snapshot.story_revision_id,
            "script_revision_id": snapshot.script_revision_id,
            "shot_plan_revision_id": snapshot.shot_plan_revision_id,
            "reference_asset_versions": SeedanceInputMapper._plain(snapshot.reference_asset_versions),
            "shots": shots,
        }

    @staticmethod
    def _plain(value: Mapping[str, object]) -> dict[str, object]:
        def thaw(item: object) -> object:
            if isinstance(item, Mapping):
                return {str(key): thaw(child) for key, child in item.items()}
            if isinstance(item, (tuple, list)):
                return [thaw(child) for child in item]
            return item

        return {str(key): thaw(item) for key, item in value.items()}


class SeedanceProductionAdapter(ProductionRuntimeAdapter):
    name = "seedance"
    submission_uncertain_on_error = True
    STATUS_MAP = {
        "waiting": "QUEUED", "queued": "QUEUED", "submitted": "QUEUED",
        "processing": "RUNNING", "running": "RUNNING", "in_progress": "RUNNING",
        "completed": "SUCCEEDED", "succeeded": "SUCCEEDED", "success": "SUCCEEDED",
        "failed": "FAILED", "error": "FAILED", "cancelled": "CANCELLED", "canceled": "CANCELLED",
    }

    def __init__(self, config: SeedanceProviderConfig | None = None, *, client: Any | None = None) -> None:
        self.config = config or SeedanceProviderConfig.from_environment()
        self._client = client

    @property
    def status(self):
        from ..ai_capabilities import CapabilityKind, CapabilityStatus

        available = bool(self.config.api_key and self.config.allow_paid_live_tests)
        return CapabilityStatus(
            CapabilityKind.VIDEO_GENERATIVE,
            "SEEDANCE",
            available,
            "configured" if available else ("provider credential unavailable" if not self.config.api_key else "paid live authorization is required"),
            {"model": self.config.model, "live_authorized": self.config.allow_paid_live_tests},
        )

    def validate(self, snapshot: ProductionInputSnapshot) -> bool:
        try:
            self.config.validate(require_live=False)
            mapped = SeedanceInputMapper.map_snapshot(snapshot)
            return bool(mapped["shots"])
        except (SeedanceAdapterError, TypeError, ValueError):
            return False

    def submit(self, snapshot: ProductionInputSnapshot) -> RuntimeSubmission:
        self.config.validate(require_live=True)
        payload = SeedanceInputMapper.map_snapshot(snapshot)
        key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        client = self._client or self._requests_client()
        response = client.post(
            self.config.base_url.rstrip("/") + "/contents/generations/tasks",
            json={"model": self.config.model, "content": payload["shots"], "metadata": {"project_id": snapshot.project_id}},
            headers={"Authorization": f"Bearer {self.config.api_key}", "X-Idempotency-Key": key},
            timeout=self.config.timeout_seconds,
        )
        data = self._response_json(response)
        reference = data.get("id") or data.get("task_id") or data.get("taskId")
        if not reference:
            raise SeedanceAdapterError("Seedance response 缺少 task identity")
        return RuntimeSubmission(str(reference), {"provider": "SEEDANCE", "model": self.config.model, "idempotency_key": key})

    def get_status(self, runtime_reference: str) -> str:
        client = self._client or self._requests_client()
        response = client.get(
            self.config.base_url.rstrip("/") + f"/contents/generations/tasks/{runtime_reference}",
            headers={"Authorization": f"Bearer {self.config.api_key}"}, timeout=self.config.timeout_seconds,
        )
        data = self._response_json(response)
        return self.map_status(data.get("status") or data.get("state"))

    def cancel(self, runtime_reference: str) -> bool:
        client = self._client or self._requests_client()
        response = client.post(
            self.config.base_url.rstrip("/") + f"/contents/generations/tasks/{runtime_reference}/cancel",
            headers={"Authorization": f"Bearer {self.config.api_key}"}, timeout=self.config.timeout_seconds,
        )
        return getattr(response, "status_code", 200) < 300

    @classmethod
    def map_status(cls, raw: object) -> str:
        value = raw.value if hasattr(raw, "value") else str(raw or "").strip().lower()
        try:
            return cls.STATUS_MAP[value.lower()]
        except KeyError as exc:
            raise SeedanceAdapterError(f"unknown Seedance status: {raw}") from exc

    @staticmethod
    def _response_json(response: Any) -> dict[str, object]:
        status = getattr(response, "status_code", 200)
        if status >= 400:
            raise SeedanceAdapterError(f"Seedance HTTP {status}")
        try:
            value = response.json()
        except Exception as exc:
            raise SeedanceAdapterError("Seedance response JSON 无效") from exc
        if not isinstance(value, dict):
            raise SeedanceAdapterError("Seedance response 不是 object")
        return value

    @staticmethod
    def _requests_client():
        import requests

        return requests


__all__ = ["SeedanceAdapterError", "SeedanceInputMapper", "SeedanceProductionAdapter", "SeedanceProviderConfig"]
