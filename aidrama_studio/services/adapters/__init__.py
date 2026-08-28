"""Runtime adapter seams; concrete integrations remain isolated behind them."""

from .mpt_runtime import MPTAdapterError, MPTInputMapper, MPTProductionAdapter
from .mock_adapter import MockProductionAdapter
from .production_adapter import (
    ProductionRuntimeAdapter,
    RuntimeContentRejectedError,
    RuntimeEvent,
    RuntimeReconciliationRequired,
    RuntimeSubmission,
    RuntimeTransientError,
)
from .wan_video import (
    WanAdapterError,
    WanFirstFrameResolver,
    WanFirstFrameSelection,
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
from .mainland_wan import MainlandWanAdapterError, MainlandWanProductionAdapter
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
    "RuntimeTransientError", "RuntimeReconciliationRequired", "RuntimeContentRejectedError",
    "MPTAdapterError", "MPTInputMapper", "MPTProductionAdapter", "MockProductionAdapter",
    "WanAdapterError", "WanInputMapper", "WanProductionAdapter", "WanPromptMapper",
    "WanFirstFrameResolver", "WanFirstFrameSelection",
    "WanProviderConfig", "WanProviderHTTPError", "WanReferenceResolver",
    "WanReferenceSelection", "WanVideoClient",
    "WanTransientError",
    "MainlandWanAdapterError", "MainlandWanProductionAdapter",
    "SeedanceAdapterError", "SeedanceInputMapper", "SeedanceProductionAdapter", "SeedanceProviderConfig", "SeedanceTransientError",
    "FinalAssemblyRenderRequest", "FinalAssemblyRuntimeAdapter", "FinalAssemblyRuntimeError", "MPTFinalAssemblyAdapter",
]
