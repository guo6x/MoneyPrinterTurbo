"""Future runtime adapter seams; adapters must not depend on the MPT runtime here."""

from .production_adapter import ProductionRuntimeAdapter

__all__ = ["ProductionRuntimeAdapter"]
