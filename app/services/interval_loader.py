"""Ordered, resumable, self-throttling loader for a Smart Tier interval.

Encodes the TNO load *playbook* as one call: it discovers a downloaded
dataset's parts, runs them in dependency order (reference-data -> Misc
master-data -> Well -> Wellbore -> work-products), and picks the right
transport per tier:

* **list tiers** (reference/master data) go through the **Storage API**
  (:func:`app.services.bulk_loader.submit_records_from_paths`) — fast,
  batched, idempotent upserts.
* **work-products** go through the **ingestion DAG**
  (:func:`app.services.work_product_loader.submit_work_products`) because
  they carry surrogate-key references and file blobs the DAG resolves — and
  they run through the shared :func:`run_concurrent_throttled` engine so the
  submit rate adapts to what the instance can take.

Every tier is stamped with the interval ``load_prefix`` so each interval is
an independent Smart Tier copy that ages on its own clock. Work-product
progress is tracked in a :class:`ResumableProgress` file so an interrupted
run resumes without re-minting duplicate records.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.models.connection import ADMEConnection
from app.models.osdu import SubmitResult
from app.services.bulk_loader import (
    DEFAULT_STORAGE_BATCH_SIZE,
    submit_records_from_paths,
)
from app.services.concurrency import (
    ItemResult,
    RunnerStats,
    ThrottlePolicy,
    is_overload_error,
    run_concurrent_throttled,
)
from app.services.downloaded_dataset import (
    DownloadedPart,
    discover_parts,
    list_part_manifests,
)
from app.services.load_progress import ResumableProgress
from app.services.work_product_loader import submit_work_products

__all__ = [
    "IntervalEvent",
    "TierPlan",
    "plan_interval",
    "run_interval",
]

# Dependency-ordered rank for a part key. Lower loads first.
_STORAGE_RANK: dict[str, int] = {
    "reference-data": 0,
    "master-data/misc_master_data": 1,
    "master-data/well": 2,
    "master-data/wellbore": 3,
}
_WP_RANK: dict[str, int] = {
    "work-products/documents": 10,
    "work-products/well logs": 11,
    "work-products/markers": 12,
    "work-products/trajectories": 13,
}


def _rank(part: DownloadedPart) -> int:
    key = part.key.lower()
    if not part.is_work_product:
        return _STORAGE_RANK.get(key, 4)  # other master-data after known ones
    return _WP_RANK.get(key, 14)


def _is_v110(part: DownloadedPart) -> bool:
    return part.key.endswith("_1_1_0")


@dataclass(frozen=True, slots=True)
class TierPlan:
    """One ordered tier to load, and how."""

    part: DownloadedPart
    method: str  # "storage" | "dag"

    @property
    def key(self) -> str:
        return self.part.key


@dataclass(frozen=True, slots=True)
class IntervalEvent:
    """Progress event streamed from :func:`run_interval`.

    ``phase`` is ``"tier_start"``, ``"item"``, or ``"tier_done"``. On
    ``item`` events ``result`` carries the per-record/manifest
    :class:`SubmitResult`. ``tier_total`` is set on ``tier_start`` and
    ``tier_ok``/``tier_failed`` on ``tier_done``.
    """

    tier: str
    method: str
    phase: str
    result: SubmitResult | None = None
    tier_total: int = 0
    tier_ok: int = 0
    tier_failed: int = 0


def plan_interval(
    root: Path, *, include_work_products: bool = True, include_v110: bool = False
) -> list[TierPlan]:
    """Return the ordered tier plan discovered under ``root``.

    Storage (list) tiers first in dependency order, then work-products.
    ``*_1_1_0`` alternate-schema parts are excluded unless ``include_v110``.
    """
    plans: list[TierPlan] = []
    for part in discover_parts(root):
        if _is_v110(part) and not include_v110:
            continue
        if part.is_work_product and not include_work_products:
            continue
        method = "dag" if part.is_work_product else "storage"
        plans.append(TierPlan(part=part, method=method))
    plans.sort(key=lambda p: (_rank(p.part), p.part.key))
    return plans


def _thread_safe_provider(
    token_provider: Callable[[], str] | None,
) -> Callable[[], str] | None:
    """Serialize token minting so concurrent workers don't race the CLI."""
    if token_provider is None:
        return None
    lock = threading.Lock()

    def _provider() -> str:
        with lock:
            return token_provider()

    return _provider


def run_interval(
    root: Path,
    *,
    interval_label: str,
    connection: ADMEConnection,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
    token: str,
    token_provider: Callable[[], str] | None = None,
    include_work_products: bool = True,
    include_v110: bool = False,
    storage_batch_size: int = DEFAULT_STORAGE_BATCH_SIZE,
    wp_policy: ThrottlePolicy | None = None,
    progress: ResumableProgress | None = None,
    should_abort: Callable[[], bool] | None = None,
    on_throttle: Callable[[str, int, int], None] | None = None,
) -> Iterator[IntervalEvent]:
    """Load a whole interval in dependency order, yielding progress events.

    ``interval_label`` becomes the ``load_prefix`` on every tier (blank =
    a single idempotent copy). Storage tiers upsert (safe to re-run);
    work-product tiers consult ``progress`` (if given) to resume without
    duplicating. ``should_abort`` is polled to stop early. ``wp_policy``
    tunes the work-product concurrency/throttle (default: max 8).
    """
    policy = wp_policy or ThrottlePolicy(max_concurrency=8)
    safe_provider = _thread_safe_provider(token_provider)

    for plan in plan_interval(
        root,
        include_work_products=include_work_products,
        include_v110=include_v110,
    ):
        if should_abort is not None and should_abort():
            return
        part = plan.part
        manifests = list_part_manifests(part)

        if plan.method == "storage":
            yield IntervalEvent(
                tier=part.key,
                method="storage",
                phase="tier_start",
                tier_total=len(manifests),
            )
            ok = failed = 0
            for result in submit_records_from_paths(
                manifests,
                section=part.section or "ReferenceData",
                acl_owners=acl_owners,
                acl_viewers=acl_viewers,
                legal_tag=legal_tag,
                data_partition_id=connection.data_partition_id,
                connection=connection,
                token=token,
                batch_size=storage_batch_size,
                load_prefix=interval_label,
                overwrite_acl_legal=True,
                token_provider=token_provider,
            ):
                if result.status == "success":
                    ok += 1
                else:
                    failed += 1
                yield IntervalEvent(
                    tier=part.key,
                    method="storage",
                    phase="item",
                    result=result,
                )
                if should_abort is not None and should_abort():
                    break
            yield IntervalEvent(
                tier=part.key,
                method="storage",
                phase="tier_done",
                tier_ok=ok,
                tier_failed=failed,
            )
        else:
            yield from _run_wp_tier(
                part,
                manifests,
                interval_label=interval_label,
                connection=connection,
                acl_owners=acl_owners,
                acl_viewers=acl_viewers,
                legal_tag=legal_tag,
                token=token,
                token_provider=safe_provider,
                policy=policy,
                progress=progress,
                should_abort=should_abort,
                on_throttle=on_throttle,
            )


def _run_wp_tier(
    part: DownloadedPart,
    manifests: Sequence[Path],
    *,
    interval_label: str,
    connection: ADMEConnection,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
    token: str,
    token_provider: Callable[[], str] | None,
    policy: ThrottlePolicy,
    progress: ResumableProgress | None,
    should_abort: Callable[[], bool] | None,
    on_throttle: Callable[[str, int, int], None] | None,
) -> Iterator[IntervalEvent]:
    remaining: list[Path] = list(manifests)
    if progress is not None:
        done = progress.completed(part.key)
        remaining = [m for m in manifests if m.name not in done]

    yield IntervalEvent(
        tier=part.key,
        method="dag",
        phase="tier_start",
        tier_total=len(remaining),
    )

    def work(manifest: Path) -> ItemResult[Path]:
        results = list(
            submit_work_products(
                [manifest],
                datasets_root=part.datasets_root,
                acl_owners=acl_owners,
                acl_viewers=acl_viewers,
                legal_tag=legal_tag,
                data_partition_id=connection.data_partition_id,
                connection=connection,
                token=token,
                load_prefix=interval_label,
                token_provider=token_provider,
            )
        )
        result = results[0] if results else None
        ok = result is not None and result.status == "success"
        error = None if result is None else result.error
        return ItemResult(
            item=manifest,
            ok=ok,
            overload=(not ok) and is_overload_error(error),
            error=error,
            payload=result,
        )

    stats = RunnerStats()
    for item_result in run_concurrent_throttled(
        remaining,
        work,
        policy=policy,
        should_abort=should_abort,
        on_throttle=(
            (lambda a, b: on_throttle(part.key, a, b))
            if on_throttle is not None
            else None
        ),
        stats=stats,
    ):
        if item_result.ok and progress is not None:
            progress.mark_and_maybe_save(part.key, item_result.item.name)
        if item_result.payload is not None:
            yield IntervalEvent(
                tier=part.key,
                method="dag",
                phase="item",
                result=item_result.payload,
            )
    if progress is not None:
        progress.save()
    yield IntervalEvent(
        tier=part.key,
        method="dag",
        phase="tier_done",
        tier_ok=stats.ok,
        tier_failed=stats.failed,
    )
