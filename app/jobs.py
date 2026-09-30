"""A tiny in-memory job registry.

A comparison can take seconds, so the HTTP request only *starts* a job and the
browser polls for progress.  That keeps the request/response cycle simple and
survives a page reload.  Jobs are deliberately not persisted: this is a
single-process teaching project, and a queue would be needless infrastructure.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import JOB_TTL_SECONDS, JOB_WORKERS, MAX_RETAINED_JOBS


@dataclass
class Job:
    run_id: str
    status: str = "queued"
    stage: str = "queued"
    progress: float = 0.0
    message: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self, include_result: bool = True) -> dict:
        payload = {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.error:
            payload["error"] = self.error
        if include_result and self.result is not None:
            payload["result"] = self.result
        return payload


class JobStore:
    """Thread-safe registry of comparison jobs."""

    def __init__(self, workers: int = JOB_WORKERS) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="arena-job")

    # -- registry ---------------------------------------------------------
    def create(self) -> Job:
        job = Job(run_id=uuid.uuid4().hex[:16])
        with self._lock:
            self._jobs[job.run_id] = job
            self._order.append(job.run_id)
            self._evict_locked()
        return job

    def get(self, run_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(run_id)

    def _evict_locked(self) -> None:
        now = time.time()
        finished = [rid for rid in self._order
                    if (job := self._jobs.get(rid)) and job.status in ("done", "error")]
        for run_id in finished:
            job = self._jobs[run_id]
            if now - job.updated_at > JOB_TTL_SECONDS:
                self._jobs.pop(run_id, None)
                if run_id in self._order:
                    self._order.remove(run_id)
        while len(self._order) > MAX_RETAINED_JOBS:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            if job and job.status in ("queued", "running"):
                break
            self._jobs.pop(oldest, None)
            self._order.remove(oldest)

    # -- progress ---------------------------------------------------------
    def update(self, run_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(run_id)
            if job is None:
                return
            for key, value in fields.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            job.updated_at = time.time()

    def progress_callback(self, run_id: str) -> Callable[[str, float, str], None]:
        def callback(stage: str, fraction: float, message: str = "") -> None:
            self.update(
                run_id,
                status="running",
                stage=stage,
                progress=max(0.0, min(1.0, fraction)),
                message=message or stage,
            )

        return callback

    # -- execution --------------------------------------------------------
    def submit(self, job: Job, work: Callable[[Callable[[str, float, str], None]], dict]) -> Future:
        def runner() -> None:
            self.update(job.run_id, status="running", stage="starting", progress=0.01)
            try:
                result = work(self.progress_callback(job.run_id))
            except Exception as exc:  # noqa: BLE001 - surfaced to the browser
                self.update(
                    job.run_id,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                    stage="failed",
                    progress=1.0,
                )
                return
            self.update(job.run_id, status="done", stage="done", progress=1.0, result=result)

        return self._pool.submit(runner)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


_store: JobStore | None = None
_store_lock = threading.Lock()


def get_store() -> JobStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = JobStore()
        return _store
