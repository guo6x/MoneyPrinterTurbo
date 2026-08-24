"""Backward-compatible import path for the concrete MPT adapter."""

from .mpt_runtime import MPTAdapterError, MPTInputMapper, MPTProductionAdapter

__all__ = ["MPTAdapterError", "MPTInputMapper", "MPTProductionAdapter"]
