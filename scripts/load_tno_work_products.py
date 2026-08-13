"""Load the TNO work-product tiers through the ingestion DAG, resumably.

Work-products (well logs / documents / markers / trajectories) can't go
through the fast Storage path yet — they use ``surrogate-key`` references
between the WorkProduct / WorkProductComponent / Dataset(File) records and
carry file blobs the DAG uploads + links. So this loads them one manifest
at a time via :func:`app.services.work_product_loader.submit_work_products`.

Because a re-submitted WP manifest mints *new* server-side record ids (the
surrogate keys resolve fresh each run), a naive re-run would duplicate
records. This script therefore records per-part progress to a JSON state
file and resumes from the next un-submitted manifest, so an interruption is
safe to restart.

Run (detached, from repo root)::

    .venv/Scripts/python.exe scripts/load_tno_work_products.py \
        --root "C:/Users/marielherzog/osdu-data/tno"

Progress + a full log are written next to this script.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

# Make the repo importable when run as a plain script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.connection import ADMEConnection, AuthMethod  # noqa: E402
from app.services.auth import (  # noqa: E402
    RefreshingTokenProvider,
    acquire_cli_token,
)
from app.services.downloaded_dataset import (  # noqa: E402
    discover_parts,
    list_part_manifests,
)
from app.services.work_product_loader import submit_work_products  # noqa: E402

# --- Instance / load configuration (marielsmrttier / opendes) -------------
ENDPOINT = "https://marielsmrttier.energy.azure.com"
TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
CLIENT_ID = "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2"
DATA_PARTITION_ID = "opendes"
TOKEN_SCOPE = "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2/.default"
LEGAL_TAG = "opendes-referencedata-legal"
ACL_OWNERS = ["data.default.owners@opendes.dataservices.energy"]
ACL_VIEWERS = ["data.default.viewers@opendes.dataservices.energy"]

# --- Concurrency + adaptive throttling ------------------------------------
# Each manifest is independent network I/O (blob upload + workflow submit),
# so we run several at once. If ADME signals overload (HTTP 429/503/502/500,
# timeouts, connection resets) we halve the in-flight limit and cool down,
# then recover slowly (AIMD) — so we never hammer or crash the instance.
DEFAULT_MAX_CONCURRENCY = 8
MIN_CONCURRENCY = 1
RECOVER_AFTER_SUCCESSES = 30  # +1 to the limit after this many clean results
THROTTLE_COOLDOWN_SECONDS = 20.0  # pause after an overload signal
HARD_PAUSE_SECONDS = 60.0  # longer pause if we're overloading even at limit=1
# Substrings in an error that mean "the instance is overloaded / rejecting",
# i.e. throttle back (as opposed to a per-record data error).
_OVERLOAD_MARKERS = (
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

# Serialize token minting across worker threads (az CLI + cache write).
_TOKEN_LOCK = threading.Lock()

# Primary WP parts, in load order. The ``*_1_1_0`` variants are alternate
# schema-version copies of the same source data and are intentionally
# excluded to avoid duplicate ingestion; pass --include-v110 to add them.
PRIMARY_PART_KEYS = [
    "work-products/documents",
    "work-products/well logs",
    "work-products/markers",
    "work-products/trajectories",
]
V110_PART_KEYS = [
    "work-products/well logs_1_1_0",
    "work-products/markers_1_1_0",
    "work-products/trajectories_1_1_0",
]

_STATE_PATH = Path(__file__).resolve().parent / ".wp_load_progress.json"
_LOG_PATH = Path(__file__).resolve().parent / "wp_load.log"

logger = logging.getLogger("tno_wp_load")


def _configure_logging() -> None:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)


def _load_state() -> dict[str, object]:
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read progress file; starting fresh.")
    return {}


def _save_state(state: dict[str, object]) -> None:
    _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        data_partition_id=DATA_PARTITION_ID,
        token_scope=TOKEN_SCOPE,
        auth_method=AuthMethod.USER_IMPERSONATION,
    )


def _is_overload(error: str | None) -> bool:
    """True when an error looks like the instance rejecting/overloaded."""
    if not error:
        return False
    low = error.lower()
    return any(marker in low for marker in _OVERLOAD_MARKERS)


def _completed_names(part_key: str, part, state: dict[str, object]) -> set[str]:
    """Return the set of already-loaded manifest names for a part.

    Migrates the old integer-index format (``key -> N`` meaning the first N
    sorted manifests were done) to the name-set format used for concurrent,
    out-of-order completion tracking.
    """
    raw = state.get(part_key)
    if isinstance(raw, list):
        return {str(n) for n in raw}
    if isinstance(raw, int) and raw > 0:
        first_n = list_part_manifests(part, limit=raw, offset=0)
        return {p.name for p in first_n}
    return set()


def _submit_one(manifest: Path, part, provider, connection):
    """Submit a single WP manifest; return (name, status, error, overload)."""
    try:
        with _TOKEN_LOCK:
            token = provider()
        results = list(
            submit_work_products(
                [manifest],
                datasets_root=part.datasets_root,
                acl_owners=ACL_OWNERS,
                acl_viewers=ACL_VIEWERS,
                legal_tag=LEGAL_TAG,
                data_partition_id=DATA_PARTITION_ID,
                connection=connection,
                token=token,
                token_provider=provider,
            )
        )
    except Exception as exc:  # noqa: BLE001 - treat as a throttle signal
        return (manifest.name, "error", f"{type(exc).__name__}: {exc}", True)
    if not results:
        return (manifest.name, "error", "no result returned", False)
    r = results[0]
    overload = r.status != "success" and _is_overload(r.error)
    return (manifest.name, r.status, r.error, overload)


def _load_part(
    part,
    provider,
    connection,
    state: dict[str, object],
    *,
    max_concurrency: int,
) -> None:
    completed = _completed_names(part.key, part, state)
    all_manifests = list_part_manifests(part)
    remaining = [m for m in all_manifests if m.name not in completed]
    if not remaining:
        logger.info(
            "[%s] already complete (%d/%d).",
            part.key,
            len(completed),
            len(all_manifests),
        )
        return
    logger.info(
        "[%s] loading %d manifests (%d already done) up to %d concurrent ...",
        part.key,
        len(remaining),
        len(completed),
        max_concurrency,
    )

    state[part.key] = sorted(completed)

    limit = max_concurrency
    ok = err = 0
    since_persist = 0
    consecutive_ok = 0
    started = time.time()
    pending = iter(remaining)
    active: dict[Future, Path] = {}

    def _persist() -> None:
        state[part.key] = sorted(completed)
        _save_state(state)

    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        def _fill() -> None:
            while len(active) < limit:
                nxt = next(pending, None)
                if nxt is None:
                    break
                active[pool.submit(
                    _submit_one, nxt, part, provider, connection
                )] = nxt

        _fill()
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            throttled = False
            for fut in done:
                active.pop(fut)
                name, status, error, overload = fut.result()
                if status == "success":
                    ok += 1
                    completed.add(name)
                    since_persist += 1
                    consecutive_ok += 1
                else:
                    err += 1
                    consecutive_ok = 0
                    logger.warning("[%s] FAIL %s: %s", part.key, name, error)
                    if overload:
                        throttled = True

                if since_persist >= 25:
                    _persist()
                    since_persist = 0
                    rate = (time.time() - started) / max(ok + err, 1)
                    logger.info(
                        "[%s] progress %d/%d done (ok=%d err=%d) "
                        "~%.2fs/manifest limit=%d",
                        part.key,
                        len(completed),
                        len(all_manifests),
                        ok,
                        err,
                        rate,
                        limit,
                    )

            if throttled:
                new_limit = max(MIN_CONCURRENCY, limit // 2)
                logger.warning(
                    "[%s] overload signal -> throttling concurrency %d -> %d, "
                    "cooling down %.0fs",
                    part.key,
                    limit,
                    new_limit,
                    THROTTLE_COOLDOWN_SECONDS,
                )
                limit = new_limit
                consecutive_ok = 0
                time.sleep(THROTTLE_COOLDOWN_SECONDS)
                # If we're already at the floor and still failing, back off hard.
                if limit == MIN_CONCURRENCY and not active and err:
                    logger.warning(
                        "[%s] still overloaded at floor; hard pause %.0fs",
                        part.key,
                        HARD_PAUSE_SECONDS,
                    )
                    time.sleep(HARD_PAUSE_SECONDS)
            elif consecutive_ok >= RECOVER_AFTER_SUCCESSES and limit < max_concurrency:
                limit += 1
                consecutive_ok = 0
                logger.info(
                    "[%s] recovered; raising concurrency -> %d", part.key, limit
                )

            _fill()

    _persist()
    logger.info(
        "[%s] DONE: ok=%d err=%d (%d/%d loaded) in %.1fs",
        part.key,
        ok,
        err,
        len(completed),
        len(all_manifests),
        time.time() - started,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        required=True,
        help="TNO download root (contains TNO/provided and datasets).",
    )
    parser.add_argument(
        "--include-v110",
        action="store_true",
        help="Also load the *_1_1_0 alternate-schema WP parts.",
    )
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated part keys to load (default: all primary).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_MAX_CONCURRENCY,
        help=(
            "Max manifests submitted concurrently. Auto-throttles down on "
            "overload (429/503/timeout) and recovers. Default "
            f"{DEFAULT_MAX_CONCURRENCY}."
        ),
    )
    args = parser.parse_args()

    _configure_logging()
    root = Path(args.root)
    parts_by_key = {p.key: p for p in discover_parts(root)}

    if args.only.strip():
        wanted = [k.strip() for k in args.only.split(",") if k.strip()]
    else:
        wanted = list(PRIMARY_PART_KEYS)
        if args.include_v110:
            wanted += V110_PART_KEYS

    provider = RefreshingTokenProvider(acquire_cli_token)
    connection = _connection()
    state = _load_state()
    max_concurrency = max(MIN_CONCURRENCY, int(args.concurrency))

    logger.info(
        "=== TNO work-product load starting: parts=%s max_concurrency=%d ===",
        wanted,
        max_concurrency,
    )
    for key in wanted:
        part = parts_by_key.get(key)
        if part is None:
            logger.warning("Part %r not found under %s; skipping.", key, root)
            continue
        if not part.is_work_product:
            logger.warning("Part %r is not a work-product; skipping.", key)
            continue
        try:
            _load_part(
                part,
                provider,
                connection,
                state,
                max_concurrency=max_concurrency,
            )
        except Exception:  # noqa: BLE001 - keep going to the next part
            logger.exception("[%s] crashed; moving on.", key)
    logger.info("=== TNO work-product load finished ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
