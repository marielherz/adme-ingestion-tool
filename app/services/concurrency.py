"""Bounded-concurrency runner with adaptive (AIMD) overload throttling.

Extracted from the one-off TNO work-product loader so the same speed +
self-protection behavior is reusable by any bulk operation (the DAG
work-product path, the Storage record path, the interval orchestrator, and
the Streamlit background jobs).

The runner submits ``work(item)`` for many items through a thread pool but
keeps the number *in flight* under an adaptive limit: on an overload signal
(the item result's :attr:`ItemResult.overload` flag) it halves the limit and
cools down (additive-increase / multiplicative-decrease), recovering by one
after a run of clean results. That way a load never hammers an instance into
failure — it paces itself to whatever the service can sustain.

``work`` runs on worker threads, so it must be thread-safe with respect to
any shared state it touches (serialize token minting, etc.). Results are
yielded as they complete — order is **not** preserved.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")

__all__ = [
    "DEFAULT_OVERLOAD_MARKERS",
    "ItemResult",
    "RunnerStats",
    "ThrottlePolicy",
    "is_overload_error",
    "run_concurrent_throttled",
]

# Substrings that mark an error as "the service is overloaded / rejecting"
# (throttle back) rather than a per-item data problem (just record + move on).
DEFAULT_OVERLOAD_MARKERS: tuple[str, ...] = (
    "429",
    "too many",
    "503",
    "service unavailable",
    "502",
    "bad gateway",
    "500",
    "timed out",
    "timeout",
    "max retries",
    "connection",
    "temporarily",
)


def is_overload_error(
    error: str | None, markers: Sequence[str] = DEFAULT_OVERLOAD_MARKERS
) -> bool:
    """True when ``error`` looks like a throttle-worthy overload signal."""
    if not error:
        return False
    low = error.lower()
    return any(marker in low for marker in markers)


@dataclass(frozen=True, slots=True)
class ItemResult(Generic[T]):
    """Outcome of ``work(item)`` for one item.

    ``ok`` marks success. ``overload`` (only meaningful when not ``ok``)
    tells the runner to throttle back rather than treat it as a plain
    failure. ``payload`` carries anything the caller wants back (a record
    id, a domain result object, etc.).
    """

    item: T
    ok: bool
    overload: bool = False
    error: str | None = None
    payload: Any = None


@dataclass(frozen=True, slots=True)
class ThrottlePolicy:
    """Knobs for the adaptive concurrency controller."""

    max_concurrency: int = 8
    min_concurrency: int = 1
    recover_after_successes: int = 30
    cooldown_seconds: float = 20.0
    hard_pause_seconds: float = 60.0

    def clamp(self, value: int) -> int:
        return max(self.min_concurrency, min(value, self.max_concurrency))


@dataclass
class RunnerStats:
    """Aggregate outcome of a :func:`run_concurrent_throttled` pass."""

    total: int = 0
    ok: int = 0
    failed: int = 0
    throttle_events: int = 0
    final_limit: int = 0
    aborted: bool = False
    durations: list[float] = field(default_factory=list)


def run_concurrent_throttled(
    items: Sequence[T],
    work: Callable[[T], ItemResult[T]],
    *,
    policy: ThrottlePolicy | None = None,
    should_abort: Callable[[], bool] | None = None,
    on_throttle: Callable[[int, int], None] | None = None,
    on_recover: Callable[[int], None] | None = None,
    stats: RunnerStats | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[ItemResult[T]]:
    """Run ``work`` over ``items`` with bounded, self-throttling concurrency.

    Yields each :class:`ItemResult` as it completes (out of order). The
    in-flight limit starts at ``policy.max_concurrency`` and adapts: an
    ``overload`` result halves it and cools down (hard-pauses if already at
    the floor), a run of ``recover_after_successes`` clean results raises it
    by one (up to the max). ``should_abort`` (checked between waves) stops
    scheduling new work and drains what's in flight. ``sleep`` is injectable
    so tests don't wait on real cooldowns. Pass ``stats`` to collect totals.
    """
    pol = policy or ThrottlePolicy()
    st = stats if stats is not None else RunnerStats()
    st.total = len(items)
    limit = pol.max_concurrency
    consecutive_ok = 0
    pending = iter(items)
    active: dict[Future[ItemResult[T]], T] = {}

    if not items:
        st.final_limit = limit
        return

    with ThreadPoolExecutor(max_workers=pol.max_concurrency) as pool:

        def _fill() -> None:
            if should_abort is not None and should_abort():
                return
            while len(active) < limit:
                nxt = next(pending, None)
                if nxt is None:
                    break
                active[pool.submit(work, nxt)] = nxt

        _fill()
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            throttled = False
            for fut in done:
                active.pop(fut)
                result = fut.result()
                if result.ok:
                    st.ok += 1
                    consecutive_ok += 1
                else:
                    st.failed += 1
                    consecutive_ok = 0
                    if result.overload:
                        throttled = True
                yield result

            if throttled:
                new_limit = pol.clamp(limit // 2)
                st.throttle_events += 1
                if on_throttle is not None:
                    on_throttle(limit, new_limit)
                limit = new_limit
                consecutive_ok = 0
                sleep(pol.cooldown_seconds)
                if limit == pol.min_concurrency and not active:
                    sleep(pol.hard_pause_seconds)
            elif (
                consecutive_ok >= pol.recover_after_successes
                and limit < pol.max_concurrency
            ):
                limit += 1
                consecutive_ok = 0
                if on_recover is not None:
                    on_recover(limit)

            if should_abort is not None and should_abort():
                st.aborted = True
                # Stop scheduling new work; let in-flight futures drain.
                pending = iter(())
            _fill()

    st.final_limit = limit
