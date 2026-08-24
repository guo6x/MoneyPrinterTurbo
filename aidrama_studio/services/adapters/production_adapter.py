from __future__ import annotations

class ProductionRuntimeAdapter:
    """Boundary for a future production executor; no runtime is wired in Task006A."""

    name = "abstract"

    def submit(self, job):
        raise NotImplementedError("Production runtime adapter is not implemented")
