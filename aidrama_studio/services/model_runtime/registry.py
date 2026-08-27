"""Registry compatibility surface for model manifests and protocol drivers."""

from .builtins import (
    BUILTIN_MANIFESTS,
    BuiltinManifestRegistry,
    builtin_manifest_registry,
    default_manifest_registry,
)
from .resolver import (
    InMemoryManifestRegistry,
    ManifestRegistry,
    ModelManifestRegistry,
    ResolverRegistry,
)
from .protocol_registry import (
    DriverRegistry,
    ProtocolDriverRegistry,
    UnsupportedProtocolError as DriverUnsupportedProtocolError,
)

__all__ = [
    "BUILTIN_MANIFESTS",
    "BuiltinManifestRegistry",
    "InMemoryManifestRegistry",
    "ModelManifestRegistry",
    "ManifestRegistry",
    "ResolverRegistry",
    "DriverRegistry",
    "ProtocolDriverRegistry",
    "DriverUnsupportedProtocolError",
    "builtin_manifest_registry",
    "default_manifest_registry",
]
