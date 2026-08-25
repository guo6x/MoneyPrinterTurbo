"""Small, provider-neutral helpers for durable human Draft editing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def _canonical(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def draft_is_dirty(revision: dict[str, Any], working: Any) -> bool:
    """Compare editable data with the last durable revision snapshot.

    Invalid/incomplete form data is considered dirty rather than raising from
    the page, so a human always gets an unsaved warning before navigation.
    """

    try:
        return _canonical(revision["content"]) != _canonical(working)
    except Exception:
        return True


@dataclass(frozen=True, slots=True)
class DraftState:
    project_id: str
    revision_id: str
    status: str
    dirty: bool
    durable: bool
    updated_at: str

    @property
    def recovery_available(self) -> bool:
        return self.durable and self.status == "DRAFT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "revision_id": self.revision_id,
            "status": self.status,
            "dirty": self.dirty,
            "durable": self.durable,
            "updated_at": self.updated_at,
            "recovery_available": self.recovery_available,
        }


def draft_state(revision: dict[str, Any], working: Any) -> DraftState:
    status = getattr(revision.get("status"), "value", revision.get("status", ""))
    return DraftState(
        project_id=str(revision["project_id"]),
        revision_id=str(revision["id"]),
        status=str(status),
        dirty=draft_is_dirty(revision, working),
        durable=True,
        updated_at=str(revision.get("updated_at") or ""),
    )


__all__ = ["DraftState", "draft_is_dirty", "draft_state"]
