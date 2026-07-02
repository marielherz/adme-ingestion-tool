"""Background load jobs — run a bulk load off the Streamlit script thread.

Streamlit executes a page's script synchronously per interaction, so a long
foreground load loop blocks the session and is abandoned the moment the
operator navigates to another page. This module runs the load on a daemon
thread and keeps its progress in a process-level registry, so the load
survives page navigation: any later render just reads the current snapshot.

Design:
- :class:`LoadJob` holds mutable progress guarded by a lock; the worker
  thread calls :meth:`LoadJob.record` per manifest and checks
  :attr:`LoadJob.aborting`.
- :func:`start_job` registers a job and spawns the worker (or runs inline
  when ``_RUN_SYNC`` is set, which the page tests use for determinism).
- The Streamlit page never touches :class:`LoadJob` internals across threads
  except through :meth:`LoadJob.snapshot` / :func:`get_job` /
  :func:`request_abort`, all lock-guarded.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.models.osdu import SubmitResult

logger = logging.getLogger(__name__)

# Job lifecycle states.
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_ABORTED = "aborted"

# Test hook: when True, ``start_job`` runs the work inline instead of on a
# thread so page tests observe a completed job deterministically.
_RUN_SYNC = False

__all__ = [
    "STATUS_ABORTED",
    "STATUS_DONE",
    "STATUS_ERROR",
    "STATUS_RUNNING",
    "LoadJob",
    "LoadJobSnapshot",
    "clear_job",
    "get_job",
    "request_abort",
    "reset",
    "start_job",
]


@dataclass(frozen=True, slots=True)
class LoadJobSnapshot:
    """Immutable view of a job's progress for the UI to render."""

    job_id: str
    label: str
    total: int
    status: str
    submitted: int
    succeeded: int
    failed: int
    error: str | None
    recent: list[SubmitResult] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float | None = None

    @property
    def is_running(self) -> bool:
        return self.status == STATUS_RUNNING


class LoadJob:
    """A single background load, with lock-guarded progress."""

    def __init__(self, job_id: str, label: str, total: int) -> None:
        self.job_id = job_id
        self.label = label
        self.total = total
        self.status = STATUS_RUNNING
        self.error: str | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None
        self._results: list[SubmitResult] = []
        self._succeeded = 0
        self._failed = 0
        self._abort = False
        self._lock = threading.Lock()

    def record(self, result: SubmitResult) -> None:
        """Append one manifest result (thread-safe)."""
        with self._lock:
            self._results.append(result)
            if result.status == "success":
                self._succeeded += 1
            else:
                self._failed += 1

    @property
    def aborting(self) -> bool:
        with self._lock:
            return self._abort

    def request_abort(self) -> None:
        with self._lock:
            self._abort = True

    def finish(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.error = error
            self.finished_at = time.time()

    def snapshot(self, *, recent: int = 25) -> LoadJobSnapshot:
        with self._lock:
            return LoadJobSnapshot(
                job_id=self.job_id,
                label=self.label,
                total=self.total,
                status=self.status,
                submitted=len(self._results),
                succeeded=self._succeeded,
                failed=self._failed,
                error=self.error,
                recent=list(self._results[-recent:]),
                started_at=self.started_at,
                finished_at=self.finished_at,
            )


_JOBS: dict[str, LoadJob] = {}
_REGISTRY_LOCK = threading.Lock()


def start_job(
    job_id: str,
    *,
    label: str,
    total: int,
    work: Callable[[LoadJob], None],
) -> LoadJob:
    """Register a job and run ``work(job)`` on a daemon thread.

    ``work`` should drive the load, calling :meth:`LoadJob.record` per
    manifest and breaking when :attr:`LoadJob.aborting` becomes true. If a
    job with ``job_id`` is already running it is returned unchanged (no
    double-start). Exceptions in ``work`` mark the job ``error``.
    """
    with _REGISTRY_LOCK:
        existing = _JOBS.get(job_id)
        if existing is not None and existing.status == STATUS_RUNNING:
            return existing
        job = LoadJob(job_id, label, total)
        _JOBS[job_id] = job

    def _run() -> None:
        try:
            work(job)
        except Exception as exc:  # noqa: BLE001 - surface any worker failure
            logger.exception("background load job %s failed", job_id)
            job.finish(STATUS_ERROR, f"{type(exc).__name__}: {exc}")
            return
        job.finish(STATUS_ABORTED if job.aborting else STATUS_DONE)

    if _RUN_SYNC:
        _run()
    else:
        threading.Thread(
            target=_run, name=f"load-{job_id}", daemon=True
        ).start()
    return job


def get_job(job_id: str) -> LoadJob | None:
    with _REGISTRY_LOCK:
        return _JOBS.get(job_id)


def request_abort(job_id: str) -> None:
    job = get_job(job_id)
    if job is not None:
        job.request_abort()


def clear_job(job_id: str) -> None:
    with _REGISTRY_LOCK:
        _JOBS.pop(job_id, None)


def reset() -> None:
    """Drop all registered jobs (tests)."""
    with _REGISTRY_LOCK:
        _JOBS.clear()
