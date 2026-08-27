"""Create-once gates and provider-neutral paid budget projections."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from aidrama_studio.domain import (
    PaidBudgetLedger,
    PaidBudgetProjection,
    PaidCreateReservation,
    PaidCreateStatus,
    ProductionExecution,
    ProviderTask,
)
from aidrama_studio.services.security import (
    sanitize_error,
    sanitize_persistent_metadata,
)
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class PaidBudgetError(RuntimeError):
    pass


class PaidBudgetExhausted(PaidBudgetError):
    pass


class PaidBudgetService:
    """Own the durable authorization ledger; provider adapters never do."""

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def authorize_job(
        self,
        project_id: str,
        production_job_id: str,
        *,
        authorization_fingerprint: str,
        planned_creates: int,
        authorized_max: int,
    ) -> PaidBudgetLedger:
        fingerprint = str(authorization_fingerprint).strip().lower()
        if len(fingerprint) != 64:
            raise PaidBudgetError("authorization fingerprint 无效")
        try:
            int(fingerprint, 16)
        except ValueError as exc:
            raise PaidBudgetError("authorization fingerprint 无效") from exc
        try:
            planned = int(planned_creates)
            maximum = int(authorized_max)
        except (TypeError, ValueError) as exc:
            raise PaidBudgetError("paid create bound 无效") from exc
        if planned < 0 or maximum < planned:
            raise PaidBudgetError("paid create bound 无效")
        now = _now()
        try:
            return self.repository.create_paid_budget_ledger(
                PaidBudgetLedger(
                    id=uuid4().hex,
                    project_id=project_id,
                    production_job_id=production_job_id,
                    authorization_fingerprint=fingerprint,
                    planned_creates=planned,
                    authorized_max=maximum,
                    created_at=now,
                    updated_at=now,
                )
            )
        except (KeyError, ValueError) as exc:
            raise PaidBudgetError(str(exc)) from exc

    def projection(
        self,
        project_id: str,
        production_job_id: str,
        *,
        execution_id: str | None = None,
        planned_fallback: int = 0,
    ) -> PaidBudgetProjection:
        ledger = self.repository.get_paid_budget_ledger(production_job_id)
        if ledger is None:
            return PaidBudgetProjection(
                project_id=project_id,
                production_job_id=production_job_id,
                execution_id=execution_id,
                planned_creates=max(0, int(planned_fallback)),
                authorized_max=0,
                consumed_creates=0,
                reserved_creates=0,
                uncertain_creates=0,
                remaining_creates=0,
            )
        if ledger.project_id != project_id:
            raise PaidBudgetError("PaidBudgetLedger 不属于该项目")
        job_reservations = self.repository.list_paid_create_reservations(
            production_job_id
        )
        reservations = (
            job_reservations
            if execution_id is None
            else [
                item
                for item in job_reservations
                if item.execution_id == execution_id
            ]
        )
        consumed = sum(
            item.status is PaidCreateStatus.CONSUMED for item in reservations
        )
        reserved = sum(
            item.status is PaidCreateStatus.RESERVED for item in reservations
        )
        uncertain = sum(
            item.status is PaidCreateStatus.UNCERTAIN for item in reservations
        )
        if execution_id is None:
            planned = ledger.planned_creates
            authorized = ledger.authorized_max
            remaining = max(authorized - consumed - reserved - uncertain, 0)
        else:
            # V1 has one immutable provider create gate per execution. The job
            # ledger remains the aggregate authorization authority.
            planned = 1
            authorized = 1
            job_used = len(job_reservations)
            job_remaining = max(ledger.authorized_max - job_used, 0)
            remaining = min(
                max(authorized - consumed - reserved - uncertain, 0),
                job_remaining,
            )
        return PaidBudgetProjection(
            project_id=project_id,
            production_job_id=production_job_id,
            execution_id=execution_id,
            planned_creates=planned,
            authorized_max=authorized,
            consumed_creates=consumed,
            reserved_creates=reserved,
            uncertain_creates=uncertain,
            remaining_creates=remaining,
        )

    def claim_create(
        self,
        task: ProviderTask,
        execution: ProductionExecution,
        *,
        require_budget: bool,
    ) -> tuple[ProviderTask, bool, PaidCreateReservation | None]:
        if task.execution_id != execution.id:
            raise PaidBudgetError("ProviderTask/ProductionExecution provenance 无效")
        reservation = None
        if require_budget:
            ledger = self.repository.get_paid_budget_ledger(
                execution.production_job_id
            )
            if ledger is None:
                raise PaidBudgetError(
                    "PAID_BUDGET_MISSING: provider create blocked before transport"
                )
            if (
                ledger.project_id != task.project_id
                or ledger.production_job_id != execution.production_job_id
            ):
                raise PaidBudgetError("PaidBudgetLedger provenance 无效")
            now = _now()
            reservation = PaidCreateReservation(
                id=uuid4().hex,
                ledger_id=ledger.id,
                project_id=task.project_id,
                production_job_id=execution.production_job_id,
                execution_id=execution.id,
                provider_task_record_id=task.id,
                idempotency_key=task.idempotency_key,
                status=PaidCreateStatus.RESERVED,
                created_at=now,
                updated_at=now,
            )
        try:
            return self.repository.claim_provider_submission(
                task.id, reservation=reservation
            )
        except ValueError as exc:
            if str(exc).startswith("PAID_BUDGET_EXHAUSTED"):
                raise PaidBudgetExhausted(str(exc)) from exc
            raise PaidBudgetError(str(exc)) from exc

    def mark_accepted(
        self,
        task: ProviderTask,
        *,
        provider_task_id: str,
        metadata: dict[str, object] | None = None,
    ) -> ProviderTask:
        safe = sanitize_persistent_metadata(dict(task.metadata) | dict(metadata or {}))
        return self.repository.update_provider_submission_outcome(
            task.id,
            state="PROVIDER_ACCEPTED",
            provider_task_id=provider_task_id,
            metadata=dict(safe) if isinstance(safe, dict) else {},
            submitted_at=_now(),
            updated_at=_now(),
            reservation_status=PaidCreateStatus.CONSUMED,
        )

    def mark_uncertain(
        self,
        task: ProviderTask,
        error: object,
        *,
        provider_task_id: str | None = None,
    ) -> ProviderTask:
        has_identity = bool(provider_task_id or task.provider_task_id)
        return self.repository.update_provider_submission_outcome(
            task.id,
            state=(
                "RECONCILIATION_REQUIRED" if has_identity else "UNCERTAIN_CREATE"
            ),
            provider_task_id=provider_task_id,
            error_message=sanitize_error(error, max_length=1000),
            updated_at=_now(),
            reservation_status=(
                PaidCreateStatus.CONSUMED
                if has_identity
                else PaidCreateStatus.UNCERTAIN
            ),
        )

    def mark_consumed(self, task: ProviderTask) -> ProviderTask:
        """Count an explicit provider response even when it rejected content."""

        return self.repository.update_provider_submission_outcome(
            task.id,
            state=task.state,
            provider_task_id=task.provider_task_id,
            metadata=dict(task.metadata),
            submitted_at=task.submitted_at or _now(),
            error_message=task.error_message,
            updated_at=_now(),
            reservation_status=PaidCreateStatus.CONSUMED,
        )

    def reconcile_startup(
        self, project_id: str | None = None
    ) -> list[ProviderTask]:
        return self.repository.mark_inflight_provider_creates_uncertain(
            project_id=project_id, updated_at=_now()
        )


__all__ = [
    "PaidBudgetError",
    "PaidBudgetExhausted",
    "PaidBudgetService",
]
