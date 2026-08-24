"""Worker seam for future production runtimes.

The placeholder intentionally has no implementation and no dependency on the
MoneyPrinterTurbo runtime.  A later phase may provide a concrete worker.
"""

from __future__ import annotations

class ProductionWorker:
    """Interface-only worker placeholder; no generation is performed."""

    worker_type = "placeholder"

    def run(self, execution):  # pragma: no cover - the seam must not execute yet
        raise NotImplementedError("ProductionWorker runtime is not implemented in Task006B")
