"""Runtime adapter contracts shared by production integrations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class RuntimeSubmission:
    runtime_reference: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def reference(self) -> str:
        return self.runtime_reference


@dataclass(frozen=True)
class RuntimeEvent:
    event_type: str
    payload: Mapping[str, object] = field(default_factory=dict)


class ProductionRuntimeAdapter:
    """Interface for a runtime engine; it performs no work by itself."""

    name = "abstract"

    def validate(self, snapshot: Any) -> bool:
        raise NotImplementedError("Production runtime adapter validation is not implemented")

    def submit(self, snapshot: Any) -> RuntimeSubmission:
        raise NotImplementedError("Production runtime adapter submission is not implemented")

    def cancel(self, runtime_reference: str) -> bool:
        raise NotImplementedError("Production runtime adapter cancellation is not implemented")

    def get_status(self, runtime_reference: str) -> str:
        raise NotImplementedError("Production runtime adapter status is not implemented")
