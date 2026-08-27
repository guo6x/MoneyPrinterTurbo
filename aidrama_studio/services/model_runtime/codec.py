"""Singular-name compatibility shim for :mod:`codecs`."""

from .codecs import *  # noqa: F403
from .codecs import __all__ as _CODECS_ALL

__all__ = _CODECS_ALL
