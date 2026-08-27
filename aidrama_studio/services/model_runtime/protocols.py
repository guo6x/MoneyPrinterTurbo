"""Compatibility import surface for universal runtime protocol drivers.

The implementation lives in :mod:`contracts` and :mod:`drivers`; this module
keeps early integrations from depending on a particular file layout.
"""

from .contracts import (
    CapabilityRequest,
    CapabilityResult,
    DriverResponse,
    DriverStatus,
    DriverSubmission,
    EncodedRequest,
    ProtocolFamily,
    ProviderTaskIdentity,
    RuntimeOutcome,
    StreamChunk,
)
from .drivers import (
    AsyncProtocolDriver,
    AsyncTaskDriver,
    AsyncTaskTransport,
    RequestResponseDriver,
    RequestResponseProtocolDriver,
    RequestResponseTransport,
    StreamDriver,
    StreamProtocolDriver,
    StreamTransport,
    ProtocolDriver,
    Driver,
)

__all__ = [
    "AsyncProtocolDriver",
    "AsyncTaskDriver",
    "AsyncTaskTransport",
    "CapabilityRequest",
    "CapabilityResult",
    "DriverResponse",
    "DriverStatus",
    "DriverSubmission",
    "EncodedRequest",
    "ProtocolFamily",
    "ProviderTaskIdentity",
    "RequestResponseDriver",
    "RequestResponseProtocolDriver",
    "RequestResponseTransport",
    "RuntimeOutcome",
    "StreamChunk",
    "StreamDriver",
    "StreamProtocolDriver",
    "StreamTransport",
    "ProtocolDriver",
    "Driver",
]
