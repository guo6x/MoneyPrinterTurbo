"""Compatibility shim for the built-in model manifest registry."""

from .registry import *  # noqa: F403
from .registry import __all__ as _REGISTRY_ALL

__all__ = _REGISTRY_ALL
