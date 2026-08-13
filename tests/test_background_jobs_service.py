"""Tests for the background load-job registry (real threads)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from app.models.osdu import SubmitResult
from app.services import background_jobs as bj


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    bj.reset()
    yield
    bj.reset()


def _result(name: str, *, ok: bool) -> SubmitResult:
    from datetime import UTC, datetime

    return SubmitResult(
        manifest_path=Path(name),
        filename=name,
        status="success" if ok else "error",
        run_id="r" if ok else None,
        record_id=None,
        error=None if ok else "boom",
        submitted_at=datetime.now(UTC),
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_start_job_runs_work_on_thread_and_records_progress() -> None:
    def work(job: bj.LoadJob) -> None:
        for i in range(3):
            job.record(_result(f"m{i}.json", ok=True))

    job = bj.start_job("job-1", label="Well", total=3, work=work)

    assert _wait_until(lambda: job.snapshot().status == bj.STATUS_DONE)
    snap = job.snapshot()
    assert snap.submitted == 3
    assert snap.succeeded == 3
    assert snap.failed == 0
    assert snap.total == 3
    assert bj.get_job("job-1") is job


def test_snapshot_counts_successes_and_failures() -> None:
    def work(job: bj.LoadJob) -> None:
        job.record(_result("a.json", ok=True))
        job.record(_result("b.json", ok=False))
        job.record(_result("c.json", ok=True))

    job = bj.start_job("job-2", label="x", total=3, work=work)
    assert _wait_until(lambda: job.snapshot().status == bj.STATUS_DONE)
    snap = job.snapshot()
    assert (snap.succeeded, snap.failed) == (2, 1)
    assert [r.filename for r in snap.recent] == ["a.json", "b.json", "c.json"]


def test_request_abort_stops_the_worker() -> None:
    gate = threading.Event()

    def work(job: bj.LoadJob) -> None:
        i = 0
        while not job.aborting:
            job.record(_result(f"m{i}.json", ok=True))
            i += 1
            gate.set()
            time.sleep(0.005)

    job = bj.start_job("job-3", label="x", total=0, work=work)
    assert gate.wait(timeout=5.0)  # worker started producing
    bj.request_abort("job-3")

    assert _wait_until(lambda: job.snapshot().status == bj.STATUS_ABORTED)
    assert job.snapshot().status == bj.STATUS_ABORTED


def test_worker_exception_marks_job_error() -> None:
    def work(job: bj.LoadJob) -> None:
        raise RuntimeError("kaboom")

    job = bj.start_job("job-4", label="x", total=1, work=work)
    assert _wait_until(lambda: job.snapshot().status == bj.STATUS_ERROR)
    snap = job.snapshot()
    assert snap.status == bj.STATUS_ERROR
    assert "kaboom" in (snap.error or "")


def test_start_job_does_not_double_start_running_job() -> None:
    release = threading.Event()
    started = threading.Event()

    def work(job: bj.LoadJob) -> None:
        started.set()
        release.wait(timeout=5.0)

    first = bj.start_job("job-5", label="x", total=1, work=work)
    assert started.wait(timeout=5.0)
    # Second start with the same id returns the SAME running job (no restart).
    second = bj.start_job(
        "job-5", label="x", total=1, work=lambda job: None
    )
    assert second is first
    release.set()
    assert _wait_until(lambda: first.snapshot().status == bj.STATUS_DONE)


def test_clear_job_removes_it() -> None:
    bj.start_job("job-6", label="x", total=0, work=lambda job: None)
    assert _wait_until(
        lambda: bj.get_job("job-6").snapshot().status != bj.STATUS_RUNNING
    )
    bj.clear_job("job-6")
    assert bj.get_job("job-6") is None


def test_run_sync_mode_runs_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bj, "_RUN_SYNC", True)

    def work(job: bj.LoadJob) -> None:
        job.record(_result("only.json", ok=True))

    job = bj.start_job("job-7", label="x", total=1, work=work)
    # Inline: already finished by the time start_job returns.
    snap = job.snapshot()
    assert snap.status == bj.STATUS_DONE
    assert snap.submitted == 1
