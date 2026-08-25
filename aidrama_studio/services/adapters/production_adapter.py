"""Runtime adapter contracts shared by production integrations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping


class RuntimeTransientError(RuntimeError):
    """A provider operation may be retried without creating a new task."""

    transient = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RuntimeReconciliationRequired(RuntimeError):
    """The original provider task must be inspected before any new submit."""


def parse_retry_after(value: object, *, now: datetime | None = None) -> float | None:
    """Parse standard delta-seconds or HTTP-date Retry-After values."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return min(3600.0, max(0.0, float(text)))
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return min(3600.0, max(0.0, (deadline - current).total_seconds()))


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
