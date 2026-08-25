"""Provider-neutral streaming source for large runtime artifacts.

The object is deliberately process-local and non-serializable.  A provider
adapter may capture an ephemeral signed result URL inside ``writer`` while
persisted metadata contains only stable, non-secret provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Callable


@dataclass(frozen=True, slots=True)
class StreamingArtifactSource:
    """Write one artifact into a storage-owned, bounded binary sink."""

    writer: Callable[[BinaryIO], object]
    max_bytes: int

    def __post_init__(self) -> None:
        if not callable(self.writer):
            raise TypeError("streaming artifact writer must be callable")
        if isinstance(self.max_bytes, bool) or int(self.max_bytes) <= 0:
            raise ValueError("streaming artifact max_bytes must be positive")

    def write_to(self, sink: BinaryIO) -> None:
        self.writer(sink)


__all__ = ["StreamingArtifactSource"]
