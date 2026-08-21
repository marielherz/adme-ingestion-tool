"""Smart Tier "thrash" suite — adversarial stress tests for the interval
loader's building blocks.

The Smart Tier engine is built from three reusable pieces:

* :func:`app.services.concurrency.run_concurrent_throttled` — the AIMD,
  bounded-concurrency runner that paces work against instance overload.
* :class:`app.services.load_progress.ResumableProgress` — the crash-safe
  per-key completion tracker that makes a load resumable.
* :func:`app.services.search.export_all_records` — cursor enumeration used to
  scan large kinds (delete / verify / dedup) past the Search 10k ceiling.

These tests hammer those pieces the way a real bulk load or a mass
delete/verify does: high item counts, storms of overload signals, aborts
mid-flight, partial-save "crashes", and Search paging while the underlying
metadata churns. Everything is deterministic — randomness is seeded and
``sleep`` is injected as a no-op — so the suite is fast and never touches a
real instance or the wall clock.
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path

import pytest

from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import CursorSearchResult, RecordSummary
from app.services import search
from app.services.concurrency import (
    ItemResult,
    RunnerStats,
    ThrottlePolicy,
    is_overload_error,
    run_concurrent_throttled,
)
from app.services.load_progress import ResumableProgress

_NOOP_SLEEP = lambda _s: None  # noqa: E731 - tiny injected sleep for tests


def _connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="opendes",
        auth_method=AuthMethod.USER_IMPERSONATION,
    )


# ===========================================================================
# Section 1 — AIMD concurrency runner under overload storms
# ===========================================================================


def _overload_work(overload_ids: set[int]):
    def work(item: int) -> ItemResult[int]:
        if item in overload_ids:
            return ItemResult(
                item=item, ok=False, overload=True, error="429 too many requests"
            )
        return ItemResult(item=item, ok=True, payload=item)

    return work


def test_thrash_every_item_processed_exactly_once_under_overload_storm() -> None:
    rng = random.Random(1234)
    items = list(range(600))
    overload = {i for i in items if rng.random() < 0.2}
    stats = RunnerStats()

    results = list(
        run_concurrent_throttled(
            items,
            _overload_work(overload),
            policy=ThrottlePolicy(max_concurrency=16),
            stats=stats,
            sleep=_NOOP_SLEEP,
        )
    )

    # No item is lost or double-counted despite the throttle churn.
    assert sorted(r.item for r in results) == items
    assert stats.total == len(items)
    assert stats.ok + stats.failed == len(items)
    assert stats.failed == len(overload)
    assert stats.throttle_events > 0  # the storm did trigger back-off
    assert 1 <= stats.final_limit <= 16


def test_thrash_never_exceeds_max_concurrency_cap() -> None:
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    rng = random.Random(7)
    overload = {i for i in range(500) if rng.random() < 0.25}

    def work(item: int) -> ItemResult[int]:
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            sum(range(200))  # brief spin so overlap is observable
        finally:
            with lock:
                state["active"] -= 1
        if item in overload:
            return ItemResult(item=item, ok=False, overload=True, error="503")
        return ItemResult(item=item, ok=True)

    list(
        run_concurrent_throttled(
            list(range(500)),
            work,
            policy=ThrottlePolicy(max_concurrency=12),
            sleep=_NOOP_SLEEP,
        )
    )
    # The hard cap is never breached, even as the adaptive limit moves.
    assert state["peak"] <= 12


def test_thrash_recovers_concurrency_after_overload_burst() -> None:
    # First 40 items overload, the remaining 200 are clean.
    overload = set(range(40))
    recoveries: list[int] = []
    stats = RunnerStats()

    list(
        run_concurrent_throttled(
            list(range(240)),
            _overload_work(overload),
            policy=ThrottlePolicy(
                max_concurrency=8, recover_after_successes=5
            ),
            on_recover=recoveries.append,
            stats=stats,
            sleep=_NOOP_SLEEP,
        )
    )
    # After the burst subsides the runner climbs back up toward the cap.
    assert recoveries, "expected at least one recovery step"
    assert stats.final_limit > ThrottlePolicy().min_concurrency
    assert max(recoveries) <= 8


def test_thrash_hard_pause_when_pinned_at_floor() -> None:
    sleeps: list[float] = []
    policy = ThrottlePolicy(
        max_concurrency=8,
        min_concurrency=1,
        cooldown_seconds=20.0,
        hard_pause_seconds=60.0,
    )

    def always_overload(item: int) -> ItemResult[int]:
        return ItemResult(item=item, ok=False, overload=True, error="timeout")

    stats = RunnerStats()
    list(
        run_concurrent_throttled(
            list(range(60)),
            always_overload,
            policy=policy,
            stats=stats,
            sleep=sleeps.append,
        )
    )
    assert stats.failed == 60
    assert stats.final_limit == policy.min_concurrency
    # Cooldown fires on every throttle; the hard pause fires once pinned at
    # the floor with nothing in flight.
    assert policy.cooldown_seconds in sleeps
    assert policy.hard_pause_seconds in sleeps


def test_thrash_abort_mid_flight_drains_without_loss() -> None:
    consumed = {"n": 0}

    def should_abort() -> bool:
        return consumed["n"] >= 50

    stats = RunnerStats()
    results: list[ItemResult[int]] = []
    for res in run_concurrent_throttled(
        list(range(2000)),
        lambda i: ItemResult(item=i, ok=True),
        policy=ThrottlePolicy(max_concurrency=8),
        should_abort=should_abort,
        stats=stats,
        sleep=_NOOP_SLEEP,
    ):
        consumed["n"] += 1
        results.append(res)

    assert stats.aborted is True
    # Stopped early (did not process all 2000)...
    assert len(results) < 2000
    # ...but only ever scheduled work once — no duplicates leaked out.
    assert len({r.item for r in results}) == len(results)


def test_thrash_metadata_churn_each_record_touched_once() -> None:
    """Simulate a mass metadata op (e.g. delete/upsert) under throttling.

    A shared fake 'instance' is mutated by worker threads. The runner must
    apply each record exactly once even while it halves/recovers concurrency.
    """
    lock = threading.Lock()
    instance: dict[str, int] = {}
    rng = random.Random(99)
    ids = [f"opendes:rec:{i}" for i in range(1000)]
    overload = {i for i in ids if rng.random() < 0.15}

    def apply(rid: str) -> ItemResult[str]:
        if rid in overload:
            return ItemResult(item=rid, ok=False, overload=True, error="429")
        with lock:
            instance[rid] = instance.get(rid, 0) + 1
        return ItemResult(item=rid, ok=True)

    stats = RunnerStats()
    list(
        run_concurrent_throttled(
            ids,
            apply,
            policy=ThrottlePolicy(max_concurrency=20),
            stats=stats,
            sleep=_NOOP_SLEEP,
        )
    )
    applied = set(ids) - overload
    assert set(instance) == applied
    assert all(count == 1 for count in instance.values())  # never double-applied
    assert stats.ok == len(applied)


def test_is_overload_error_markers() -> None:
    assert is_overload_error("HTTP 429: too many requests")
    assert is_overload_error("Connection reset by peer")
    assert is_overload_error("503 service unavailable")
    assert not is_overload_error("400 bad request: invalid legal tag")
    assert not is_overload_error(None)


# ===========================================================================
# Section 2 — ResumableProgress integrity under high volume + crashes
# ===========================================================================


def test_thrash_high_volume_mark_and_resume(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    names = [f"manifest-{i}.json" for i in range(1500)]
    progress = ResumableProgress(path, save_every=250)
    for name in names:
        progress.mark_and_maybe_save("well logs", name)
    progress.save()

    # A fresh instance resumes from disk with the full set, nothing pending.
    resumed = ResumableProgress(path, save_every=250)
    assert resumed.count("well logs") == 1500
    assert resumed.remaining("well logs", names) == []


def test_thrash_resume_after_partial_save_reprocesses_only_unsaved(
    tmp_path: Path,
) -> None:
    path = tmp_path / "progress.json"
    names = [f"m-{i:04d}" for i in range(250)]
    progress = ResumableProgress(path, save_every=100)
    for name in names:
        progress.mark_and_maybe_save("markers", name)
    # No final save() — simulate a crash. Auto-saves fired at 100 and 200.

    resumed = ResumableProgress(path, save_every=100)
    done = resumed.completed("markers")
    assert len(done) == 200
    # Exactly the last 50 (the unsaved tail) resume as "remaining" — they get
    # re-done, which is safe because the loaders are idempotent.
    remaining = resumed.remaining("markers", names)
    assert remaining == names[200:]


def test_thrash_save_snapshot_is_valid_json_with_all_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "progress.json"
    progress = ResumableProgress(path, save_every=10_000)
    keys = [f"tier-{k}" for k in range(50)]
    for key in keys:
        for i in range(100):
            progress.mark(key, f"{key}-item-{i}")
    progress.save()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == set(keys)
    assert all(len(v) == 100 for v in data.values())
    # Round-trips exactly.
    resumed = ResumableProgress(path)
    for key in keys:
        assert resumed.count(key) == 100


def test_thrash_legacy_int_progress_migrates_under_resume(
    tmp_path: Path,
) -> None:
    path = tmp_path / "progress.json"
    # Legacy shape: an integer count instead of a name list.
    path.write_text(json.dumps({"trajectories": 3}), encoding="utf-8")
    names = [f"t-{i}" for i in range(10)]

    progress = ResumableProgress(path)
    done = progress.completed("trajectories", migrate_int=names)
    # First 3 names treated as done; the rest remain.
    assert done == set(names[:3])
    assert progress.remaining("trajectories", names) == names[3:]


# ===========================================================================
# Section 3 — Search cursor enumeration under metadata churn
# ===========================================================================


def _summary(rid: str, kind: str) -> RecordSummary:
    return RecordSummary(id=rid, kind=kind)


def test_thrash_export_all_records_covers_stable_core_under_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor-scroll a kind while records are added/removed between pages.

    A real cursor scan is a snapshot, but overlapping pages can re-emit ids.
    The consumer must be able to build a complete, de-duplicated set of the
    records present for the whole scan without loss or crash.
    """
    kind = "osdu:wks:dataset--File.Generic:1.0.0"
    core = [f"core-{i}" for i in range(120)]
    rng = random.Random(2026)
    page_size = 10

    def fake_cursor(
        connection, token, *, kind, query=None, limit, cursor=None,
        returned_fields=None,
    ) -> CursorSearchResult:
        idx = int(cursor) if cursor else 0
        start = idx * page_size
        # Churn: sprinkle transient ids and re-emit one id from the prior page
        # (overlap) so the consumer must de-duplicate.
        window = core[start : start + page_size]
        churn = [f"churn-{rng.randint(0, 999)}" for _ in range(2)]
        if start > 0:
            window = [core[start - 1], *window]  # overlap duplicate
        page_ids = window + churn
        has_more = (start + page_size) < len(core)
        next_cursor = str(idx + 1) if has_more else None
        return CursorSearchResult(
            kind=kind,
            cursor=next_cursor,
            limit=limit,
            records=[_summary(r, kind) for r in page_ids],
            has_more=has_more,
            ok=True,
            http_status=200,
        )

    monkeypatch.setattr(search, "search_with_cursor", fake_cursor)

    seen: set[str] = set()
    pages = 0
    for page in search.export_all_records(
        _connection(), "tok", kind=kind, limit=page_size
    ):
        assert page.ok
        seen.update(r.id for r in page.records)
        pages += 1

    # Every stable-core record was enumerated at least once.
    assert set(core).issubset(seen)
    assert pages == (len(core) + page_size - 1) // page_size


def test_thrash_export_all_records_stops_and_surfaces_error_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = "osdu:wks:master-data--Well:1.0.0"
    calls = {"n": 0}

    def flaky_cursor(
        connection, token, *, kind, query=None, limit, cursor=None,
        returned_fields=None,
    ) -> CursorSearchResult:
        calls["n"] += 1
        if calls["n"] == 3:
            # A transport failure mid-scan surfaces as an error page.
            return CursorSearchResult(
                kind=kind, cursor=None, ok=False, http_status=503,
                error_message="service unavailable",
            )
        return CursorSearchResult(
            kind=kind,
            cursor=str(calls["n"]),
            records=[_summary(f"w-{calls['n']}", kind)],
            has_more=True,
            ok=True,
            http_status=200,
        )

    monkeypatch.setattr(search, "search_with_cursor", flaky_cursor)

    pages = list(
        search.export_all_records(_connection(), "tok", kind=kind, limit=1)
    )
    # The error page is yielded (so the caller sees it) and iteration stops.
    assert pages[-1].ok is False
    assert pages[-1].http_status == 503
    assert calls["n"] == 3
