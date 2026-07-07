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
import time
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


def _load_state() -> dict[str, int]:
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read progress file; starting fresh.")
    return {}


def _save_state(state: dict[str, int]) -> None:
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


def _load_part(part, provider, connection, state: dict[str, int]) -> None:
    done = int(state.get(part.key, 0))
    manifests = list_part_manifests(part, offset=done)
    if not manifests:
        logger.info("[%s] already complete (%d manifests).", part.key, done)
        return
    logger.info(
        "[%s] loading %d manifests (resuming from #%d) ...",
        part.key,
        len(manifests),
        done + 1,
    )
    ok = err = 0
    started = time.time()
    for result in submit_work_products(
        manifests,
        datasets_root=part.datasets_root,
        acl_owners=ACL_OWNERS,
        acl_viewers=ACL_VIEWERS,
        legal_tag=LEGAL_TAG,
        data_partition_id=DATA_PARTITION_ID,
        connection=connection,
        token=provider(),
        token_provider=provider,
    ):
        done += 1
        if result.status == "success":
            ok += 1
        else:
            err += 1
            logger.warning(
                "[%s] FAIL %s: %s", part.key, result.filename, result.error
            )
        state[part.key] = done
        # Persist progress every 25 manifests (and always at the end).
        if done % 25 == 0:
            _save_state(state)
            rate = (time.time() - started) / max(ok + err, 1)
            logger.info(
                "[%s] progress %d done (ok=%d err=%d) ~%.1fs/manifest",
                part.key,
                done,
                ok,
                err,
                rate,
            )
    _save_state(state)
    logger.info(
        "[%s] DONE: ok=%d err=%d in %.1fs",
        part.key,
        ok,
        err,
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

    logger.info("=== TNO work-product load starting: parts=%s ===", wanted)
    for key in wanted:
        part = parts_by_key.get(key)
        if part is None:
            logger.warning("Part %r not found under %s; skipping.", key, root)
            continue
        if not part.is_work_product:
            logger.warning("Part %r is not a work-product; skipping.", key)
            continue
        try:
            _load_part(part, provider, connection, state)
        except Exception:  # noqa: BLE001 - keep going to the next part
            logger.exception("[%s] crashed; moving on.", key)
    logger.info("=== TNO work-product load finished ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
