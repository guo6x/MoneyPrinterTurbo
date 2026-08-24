"""MoneyPrinterTurbo runtime seam.

Only an injected runtime object may be used here.  This skeleton intentionally
does not import or invoke ``app``/MPT runtime code; a later integration can
provide that object behind this adapter without changing AIDrama services.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .production_adapter import ProductionRuntimeAdapter, RuntimeSubmission


class MPTProductionAdapter(ProductionRuntimeAdapter):
    name = "mpt"

    def __init__(self, runtime: Any | None = None):
        self._runtime = runtime

    def _require_runtime(self) -> Any:
        if self._runtime is None:
            raise NotImplementedError("MPT runtime adapter is a boundary skeleton only")
        return self._runtime

    def validate(self, snapshot: Any) -> bool:
        runtime = self._require_runtime()
        result = runtime.validate(snapshot)
        return result is not False

    def submit(self, snapshot: Any) -> RuntimeSubmission:
        runtime = self._require_runtime()
        result = runtime.submit(snapshot)
        if isinstance(result, RuntimeSubmission):
            return result
        if isinstance(result, Mapping):
            reference = result.get("runtime_reference") or result.get("reference")
            if reference:
                return RuntimeSubmission(runtime_reference=str(reference), metadata=dict(result.get("metadata") or {}))
        return RuntimeSubmission(runtime_reference=str(result))

    def cancel(self, runtime_reference: str) -> bool:
        result = self._require_runtime().cancel(runtime_reference)
        return result is not False

    def get_status(self, runtime_reference: str) -> str:
        return str(self._require_runtime().get_status(runtime_reference))
