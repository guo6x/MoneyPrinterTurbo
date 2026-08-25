"""Desktop-owned durable production runner lifecycle."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from loguru import logger

from aidrama_studio.services.background_runner import (
    BackgroundProductionRunner,
    SingleInstanceGuard,
)
from aidrama_studio.services.heavy_job_runner import HeavyJobRunner
from aidrama_studio.services.production_execution import ProductionExecutionService
from aidrama_studio.services.production_worker import ProductionWorker
from aidrama_studio.services.security import sanitize_error
from aidrama_studio.storage.repositories import ProjectRepository


class DesktopBackgroundError(RuntimeError):
    pass


@dataclass
class DesktopBackgroundRunnerHost:
    """Own one runner for the lifetime of the desktop process.

    The UI process only enqueues durable work.  This host survives Streamlit
    reruns, reconciles interrupted local work on startup, and retains one
    writable-owner lock for the data directory until clean shutdown.
    """

    repository: ProjectRepository = field(default_factory=ProjectRepository)
    interval_seconds: float = 2.0
    runner_factory: Callable[..., BackgroundProductionRunner] = BackgroundProductionRunner
    heavy_runner_factory: Callable[..., HeavyJobRunner] = HeavyJobRunner
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _runner: BackgroundProductionRunner | None = field(default=None, init=False, repr=False)
    _heavy_runner: HeavyJobRunner | None = field(default=None, init=False, repr=False)
    _instance_guard: SingleInstanceGuard | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self.interval_seconds <= 0:
            raise DesktopBackgroundError("background runner interval 必须为正数")
        guard = SingleInstanceGuard(self.repository.paths.root, "desktop-instance.lock")
        try:
            guard.acquire()
        except Exception as exc:
            raise DesktopBackgroundError("该 AIDrama 数据目录已由另一个桌面实例使用") from exc
        self._instance_guard = guard
        self._stop_event.clear()
        execution_service = ProductionExecutionService(self.repository)

        def worker_factory() -> ProductionWorker:
            return ProductionWorker(
                execution_service,
                poll_interval=5.0,
                max_polls=360,
                should_stop=self._stop_event.is_set,
            )

        try:
            self._runner = self.runner_factory(
                self.repository,
                worker_factory=worker_factory,
            )
            self._heavy_runner = self.heavy_runner_factory(self.repository)
            for project in self.repository.list_projects():
                self._runner.reconcile(project.id)
            self._heavy_runner.reconcile()
            self._thread = threading.Thread(
                target=self._run,
                name="AIDramaProductionRunner",
                daemon=True,
            )
            self._thread.start()
        except Exception:
            guard.release()
            self._instance_guard = None
            self._runner = None
            self._heavy_runner = None
            raise

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._runner is not None:
                    self._runner.run_once()
                if self._heavy_runner is not None:
                    self._heavy_runner.run_once()
            except Exception as exc:
                logger.warning("AIDrama background runner cycle failed: {}", sanitize_error(exc))
            self._stop_event.wait(self.interval_seconds)

    def stop(self, timeout_seconds: float = 35.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout_seconds))
        if thread is not None and thread.is_alive():
            logger.warning("AIDrama background runner is still finishing a bounded provider call; instance lock retained")
            return
        self._thread = None
        self._runner = None
        self._heavy_runner = None
        guard, self._instance_guard = self._instance_guard, None
        if guard is not None:
            guard.release()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


__all__ = ["DesktopBackgroundError", "DesktopBackgroundRunnerHost"]
