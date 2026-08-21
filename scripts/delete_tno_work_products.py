"""Delete the TNO work-product records (WorkProduct + WPC + File.Generic).

Why: the existing WP records were loaded through the DAG dataset path, which
never promoted their blobs out of the staging area — so their bulk data is
gone (downloadURL 404s) even though the metadata records exist. To make bulk
retrievable we reload with the FIXED ``submit_work_products`` (File-service
metadata path). This script clears the old, broken records first so the
reload starts from a clean slate instead of piling up duplicates.

ONLY the work-product tiers are touched. Reference-data and master-data
(Well, Wellbore, Field, ...) are NEVER enumerated or deleted.

Two modes:

* ``--dry-run`` (DEFAULT): enumerate every target record id via cursor
  search, write them to ``scripts/.wp_delete_ids.json``, print per-kind
  counts. Makes NO changes.
* ``--execute``: delete each enumerated id via Storage soft-delete, with
  adaptive throttling, resumable progress, token auto-refresh, and a log.

Deletes are logical (soft) deletes; the reload mints fresh server-side ids
so there is no id collision and no purge is required.

Run (from repo root)::

    .venv/Scripts/python.exe scripts/delete_tno_work_products.py            # dry-run
    .venv/Scripts/python.exe scripts/delete_tno_work_products.py --execute  # delete
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from pathlib import Path

import requests

# Make the repo importable when run as a plain script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.services import settings_store  # noqa: E402
from app.services.auth import (  # noqa: E402
    RefreshingTokenProvider,
    acquire_cli_token,
)
from app.services.concurrency import (  # noqa: E402
    ItemResult,
    RunnerStats,
    ThrottlePolicy,
    is_overload_error,
    run_concurrent_throttled,
)
from app.services.load_progress import ResumableProgress  # noqa: E402
from app.services.search import export_all_records  # noqa: E402

# --- Target kinds (work-products ONLY) ------------------------------------
# Order matters for tidiness (parents first), though Storage soft-delete does
# not enforce referential integrity.
WP_KINDS: list[tuple[str, str]] = [
    ("WorkProduct", "osdu:wks:work-product--WorkProduct:*"),
    ("Document", "osdu:wks:work-product-component--Document:*"),
    ("WellLog", "osdu:wks:work-product-component--WellLog:*"),
    ("WellboreMarkerSet", "osdu:wks:work-product-component--WellboreMarkerSet:*"),
    ("WellboreTrajectory", "osdu:wks:work-product-component--WellboreTrajectory:*"),
    ("FileGeneric", "osdu:wks:dataset--File.Generic:*"),
]

# Explicit guard: refuse to run if a kind ever names reference/master data.
_FORBIDDEN = ("reference-data--", "master-data--")

_IDS_PATH = _REPO_ROOT / "scripts" / ".wp_delete_ids.json"
_PROGRESS_PATH = _REPO_ROOT / "scripts" / ".wp_delete_progress.json"
_LOG_PATH = _REPO_ROOT / "scripts" / "wp_delete.log"

_STORAGE_RECORD_PATH = "/api/storage/v2/records/{rid}"
_ENUM_PAGE = 1000
_DELETE_TIMEOUT = 30

logger = logging.getLogger("wp_delete")
_TOKEN_LOCK = threading.Lock()
_SESSION: requests.Session | None = None


def _build_session(pool_size: int) -> requests.Session:
    """A shared session with a connection pool sized to the concurrency.

    Reusing keep-alive connections avoids a fresh TLS handshake per delete,
    which is the dominant cost when firing tens of thousands of small
    requests at the same host.
    """
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=0,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(_LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _assert_safe_kinds() -> None:
    for _label, kind in WP_KINDS:
        if any(bad in kind for bad in _FORBIDDEN):
            raise SystemExit(f"Refusing to run: unsafe kind {kind!r}")


def _load_connection():
    name = settings_store.get_active_connection_name()
    conn = settings_store.load_connection(name)
    if conn is None:
        raise SystemExit("No active connection configured.")
    return name, conn


def enumerate_ids(conn, token_provider) -> dict[str, list[str]]:
    """Cursor-scroll every target kind; return {label: [record_id, ...]}."""
    all_ids: dict[str, list[str]] = {}
    for label, kind in WP_KINDS:
        ids: list[str] = []
        with _TOKEN_LOCK:
            token = token_provider()
        for page in export_all_records(
            conn, token, kind=kind, limit=_ENUM_PAGE, returned_fields=("id", "kind")
        ):
            if not page.ok:
                logger.error(
                    "Enumeration failed for %s: HTTP %s %s",
                    kind,
                    page.http_status,
                    page.error_message,
                )
                break
            ids.extend(rec.id for rec in page.records if rec.id)
        all_ids[label] = ids
        logger.info("Enumerated %s: %d ids", label, len(ids))
    return all_ids


def _delete_one(conn, provider, base: str, rid: str) -> ItemResult[str]:
    with _TOKEN_LOCK:
        token = provider()
    headers = {
        "Authorization": f"Bearer {token}",
        "data-partition-id": conn.data_partition_id,
    }
    url = base + _STORAGE_RECORD_PATH.format(rid=rid.rstrip(":"))
    client = _SESSION or requests
    try:
        resp = client.delete(url, headers=headers, timeout=_DELETE_TIMEOUT)
    except requests.RequestException as exc:
        return ItemResult(item=rid, ok=False, overload=True, error=str(exc))
    if resp.status_code in (204, 404):  # 404 => already gone (idempotent)
        return ItemResult(item=rid, ok=True)
    err = f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
    return ItemResult(
        item=rid, ok=False, overload=is_overload_error(err), error=err
    )


def execute_deletes(
    conn, provider, all_ids: dict[str, list[str]], *, max_concurrency: int = 8
) -> None:
    global _SESSION
    base = conn.endpoint.rstrip("/")
    _SESSION = _build_session(max_concurrency)
    progress = ResumableProgress(_PROGRESS_PATH, save_every=100)
    policy = ThrottlePolicy(max_concurrency=max_concurrency)
    grand_ok = 0
    grand_fail = 0
    for label, _kind in WP_KINDS:
        ids = all_ids.get(label, [])
        done = progress.completed(label)
        todo = [rid for rid in ids if rid not in done]
        logger.info(
            "%s: %d total, %d already deleted, %d to delete",
            label,
            len(ids),
            len(ids) - len(todo),
            len(todo),
        )
        if not todo:
            continue
        stats = RunnerStats()
        deleted_here = 0
        for res in run_concurrent_throttled(
            todo,
            lambda rid: _delete_one(conn, provider, base, rid),
            policy=policy,
            on_throttle=lambda new, old: logger.warning(
                "Throttle: concurrency %d -> %d", old, new
            ),
            stats=stats,
        ):
            if res.ok:
                try:
                    progress.mark_and_maybe_save(label, res.item)
                except PermissionError:
                    # A transient file lock (e.g. AV scan / concurrent read)
                    # must not abort the run — the delete already happened and
                    # is idempotent (a re-run gets 404 = ok).
                    progress.mark(label, res.item)
                grand_ok += 1
                deleted_here += 1
                if deleted_here % 1000 == 0:
                    logger.info(
                        "%s progress: %d/%d", label, deleted_here, len(todo)
                    )
            else:
                grand_fail += 1
                logger.error("Delete failed %s: %s", res.item, res.error)
        try:
            progress.save()
        except PermissionError:
            logger.warning("Could not persist progress for %s (retrying next).", label)
        logger.info(
            "%s done: ok=%d failed=%d (final limit %d)",
            label,
            stats.ok,
            stats.failed,
            stats.final_limit,
        )
    logger.info("ALL DONE: deleted=%d failed=%d", grand_ok, grand_fail)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete (default is a read-only dry-run enumeration).",
    )
    parser.add_argument(
        "--reuse-ids",
        action="store_true",
        help="Reuse a previously enumerated scripts/.wp_delete_ids.json "
        "instead of re-enumerating.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Max concurrent delete requests (adaptive; default 8).",
    )
    args = parser.parse_args()

    _configure_logging()
    _assert_safe_kinds()
    name, conn = _load_connection()
    logger.info(
        "Connection %r -> %s / %s", name, conn.endpoint, conn.data_partition_id
    )
    provider = RefreshingTokenProvider(acquire=acquire_cli_token)

    if args.reuse_ids and _IDS_PATH.exists():
        all_ids = json.loads(_IDS_PATH.read_text(encoding="utf-8"))
        logger.info("Reusing %s", _IDS_PATH)
    else:
        all_ids = enumerate_ids(conn, provider)
        _IDS_PATH.write_text(json.dumps(all_ids), encoding="utf-8")
        logger.info("Wrote ids to %s", _IDS_PATH)

    total = sum(len(v) for v in all_ids.values())
    print("\n=== TARGET RECORD COUNTS ===")
    for label, _kind in WP_KINDS:
        print(f"  {label:<22} {len(all_ids.get(label, [])):>8}")
    print(f"  {'TOTAL':<22} {total:>8}")

    if not args.execute:
        print("\nDRY-RUN: no records deleted. Re-run with --execute to delete.")
        return

    print(f"\nEXECUTE: deleting {total} records...")
    execute_deletes(conn, provider, all_ids, max_concurrency=args.concurrency)


if __name__ == "__main__":
    main()
