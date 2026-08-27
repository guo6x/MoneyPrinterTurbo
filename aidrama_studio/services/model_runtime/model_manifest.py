"""Compatibility shim for immutable model manifest types."""

from .manifest import *  # noqa: F403
from .manifest import __all__ as _MANIFEST_ALL

__all__ = _MANIFEST_ALL
