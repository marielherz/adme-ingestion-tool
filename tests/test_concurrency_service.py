"""Tests for the concurrency runner (bounded + AIMD throttling)."""

from __future__ import annotations

import threading

from app.services.concurrency import (
    ItemResult,
    RunnerStats,
    ThrottlePolicy,
    is_overload_error,
    run_concurrent_throttled,
)


def _ok(item: int) -> ItemResult[int]:
    return ItemResult(item=item, ok=True, payload=item * 10)


def test_runs_every_item_and_yields_results() -> None:
    items = list(range(20))
    stats = RunnerStats()
    results = list(
        run_concurrent_throttled(
            items,
            _ok,
            policy=ThrottlePolicy(max_concurrency=4),
            stats=stats,
            sleep=lambda _s: None,
        )
    )
    assert stats.total == 20
    assert stats.ok == 20
    assert stats.failed == 0
    # Every item processed exactly once (order not guaranteed).
    assert sorted(r.item for r in results) == items
    assert all(r.ok for r in results)


def test_empty_items_is_noop() -> None:
    stats = RunnerStats()
    results = list(
        run_concurrent_throttled([], _ok, stats=stats, sleep=lambda _s: None)
    )
    assert results == []
    assert stats.total == 0


def test_never_exceeds_max_concurrency() -> None:
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def work(item: int) -> ItemResult[int]:
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            # brief spin so overlap is observable
            sum(range(1000))
        finally:
            with lock:
                state["active"] -= 1
        return ItemResult(item=item, ok=True)

    list(
        run_concurrent_throttled(
            list(range(50)),
            work,
            policy=ThrottlePolicy(max_concurrency=5),
            sleep=lambda _s: None,
        )
    )
    assert state["peak"] <= 5


def test_overload_throttles_and_cools_down() -> None:
    sleeps: list[float] = []
    throttles: list[tuple[int, int]] = []
    # First few items report overload, then everything succeeds.
    overloaded = {0, 1, 2}

    def work(item: int) -> ItemResult[int]:
        if item in overloaded:
            return ItemResult(item=item, ok=False, overload=True, error="503")
        return ItemResult(item=item, ok=True)

    stats = RunnerStats()
    list(
        run_concurrent_throttled(
            list(range(40)),
            work,
            policy=ThrottlePolicy(
                max_concurrency=8, cooldown_seconds=20, hard_pause_seconds=60
            ),
            on_throttle=lambda a, b: throttles.append((a, b)),
            stats=stats,
            sleep=sleeps.append,
        )
    )
    assert stats.throttle_events >= 1
    assert throttles  # at least one throttle-down happened
    # A throttle halves the limit.
    old, new = throttles[0]
    assert new == max(1, old // 2)
    # Cooldown sleep was invoked (never a real wait in tests).
    assert 20 in sleeps


def test_recover_raises_limit_after_clean_run() -> None:
    recovers: list[int] = []
    # Item 0 overloads to drop the limit; the rest succeed to trigger recovery.
    def work(item: int) -> ItemResult[int]:
        if item == 0:
            return ItemResult(item=item, ok=False, overload=True, error="429")
        return ItemResult(item=item, ok=True)

    list(
        run_concurrent_throttled(
            list(range(60)),
            work,
            policy=ThrottlePolicy(
                max_concurrency=8, recover_after_successes=5
            ),
            on_recover=recovers.append,
            sleep=lambda _s: None,
        )
    )
    # After the drop, a run of clean results raises the limit again.
    assert recovers
    assert recovers == sorted(recovers)  # monotonic increases


def test_abort_stops_scheduling_new_work() -> None:
    processed: list[int] = []
    lock = threading.Lock()

    def work(item: int) -> ItemResult[int]:
        with lock:
            processed.append(item)
        return ItemResult(item=item, ok=True)

    # Abort as soon as anything has been processed.
    def should_abort() -> bool:
        with lock:
            return len(processed) >= 3

    stats = RunnerStats()
    list(
        run_concurrent_throttled(
            list(range(1000)),
            work,
            policy=ThrottlePolicy(max_concurrency=2),
            should_abort=should_abort,
            stats=stats,
            sleep=lambda _s: None,
        )
    )
    assert stats.aborted is True
    # Far fewer than all 1000 items ran because we stopped scheduling.
    assert len(processed) < 1000


def test_is_overload_error_matches_markers() -> None:
    assert is_overload_error("HTTP 503 Service Unavailable")
    assert is_overload_error("Request timed out after 30s")
    assert is_overload_error("Connection aborted")
    assert not is_overload_error("Record otherRelevantDataCountries empty")
    assert not is_overload_error(None)
    assert not is_overload_error("")
