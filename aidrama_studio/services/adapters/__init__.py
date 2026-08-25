"""Runtime adapter seams; concrete integrations remain isolated behind them."""

from .mpt_runtime import MPTAdapterError, MPTInputMapper, MPTProductionAdapter
from .mock_adapter import MockProductionAdapter
from .production_adapter import (
    ProductionRuntimeAdapter,
    RuntimeEvent,
    RuntimeReconciliationRequired,
    RuntimeSubmission,
    RuntimeTransientError,
)
from .wan_video import (
    WanAdapterError,
    WanInputMapper,
    WanProductionAdapter,
    WanPromptMapper,
    WanProviderConfig,
    WanProviderHTTPError,
    WanReferenceResolver,
    WanReferenceSelection,
    WanVideoClient,
    WanTransientError,
)
from .seedance_video import (
    SeedanceAdapterError,
    SeedanceInputMapper,
    SeedanceProductionAdapter,
    SeedanceProviderConfig,
    SeedanceTransientError,
)
from .final_assembly_runtime import (
    FinalAssemblyRenderRequest,
    FinalAssemblyRuntimeAdapter,
    FinalAssemblyRuntimeError,
    MPTFinalAssemblyAdapter,
)

__all__ = [
    "ProductionRuntimeAdapter", "RuntimeEvent", "RuntimeSubmission",
    "RuntimeTransientError", "RuntimeReconciliationRequired",
    "MPTAdapterError", "MPTInputMapper", "MPTProductionAdapter", "MockProductionAdapter",
    "WanAdapterError", "WanInputMapper", "WanProductionAdapter", "WanPromptMapper",
    "WanProviderConfig", "WanProviderHTTPError", "WanReferenceResolver",
    "WanReferenceSelection", "WanVideoClient",
    "WanTransientError",
    "SeedanceAdapterError", "SeedanceInputMapper", "SeedanceProductionAdapter", "SeedanceProviderConfig", "SeedanceTransientError",
    "FinalAssemblyRenderRequest", "FinalAssemblyRuntimeAdapter", "FinalAssemblyRuntimeError", "MPTFinalAssemblyAdapter",
]
