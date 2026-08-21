"""Load generated Volve manifests into marielsmrttier/opendes.

Run after scripts/prepare_volve_data.py::

    .venv/Scripts/python.exe scripts/load_volve_generated.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.connection import ADMEConnection, AuthMethod  # noqa: E402
from app.services.auth import RefreshingTokenProvider, acquire_cli_token  # noqa: E402
from app.services.bulk_loader import DEFAULT_STORAGE_BATCH_SIZE  # noqa: E402
from app.services.interval_loader import run_interval  # noqa: E402
from app.services.load_progress import ResumableProgress  # noqa: E402

ENDPOINT = "https://marielsmrttier.energy.azure.com"
TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
CLIENT_ID = "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2"
DATA_PARTITION_ID = "opendes"
TOKEN_SCOPE = "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2/.default"
LEGAL_TAG = "opendes-referencedata-legal"
ACL_OWNERS = ["data.default.owners@opendes.dataservices.energy"]
ACL_VIEWERS = ["data.default.viewers@opendes.dataservices.energy"]
DEFAULT_ROOT = Path.home() / "osdu-data" / "volve" / "generated-json"
_PROGRESS_PATH = Path(__file__).resolve().parent / ".volve_load_progress.json"
_LOG_PATH = Path(__file__).resolve().parent / "volve_load.log"

logger = logging.getLogger("volve_load")


def main() -> int:
    parser = argparse.ArgumentParser(description="Load generated Volve manifests")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_STORAGE_BATCH_SIZE)
    args = parser.parse_args()

    _configure_logging()
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Generated Volve root not found: {root}")

    connection = ADMEConnection(
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        data_partition_id=DATA_PARTITION_ID,
        token_scope=TOKEN_SCOPE,
        auth_method=AuthMethod.USER_IMPERSONATION,
    )
    provider = RefreshingTokenProvider(acquire=acquire_cli_token)
    token = provider()
    progress = ResumableProgress(_PROGRESS_PATH)

    logger.info("Loading Volve from %s", root)
    ok = failed = 0
    current_tier = ""
    for event in run_interval(
        root,
        interval_label="",
        connection=connection,
        acl_owners=ACL_OWNERS,
        acl_viewers=ACL_VIEWERS,
        legal_tag=LEGAL_TAG,
        token=token,
        token_provider=provider,
        include_work_products=not args.metadata_only,
        storage_batch_size=args.batch_size,
        progress=progress,
    ):
        if event.phase == "tier_start":
            current_tier = event.tier
            logger.info("[%s] start via %s (%d items)", event.tier, event.method, event.tier_total)
        elif event.phase == "item" and event.result is not None:
            result = event.result
            if result.status == "success":
                ok += 1
                logger.info("[%s] OK %s run=%s record=%s", current_tier, result.filename, result.run_id, result.record_id)
            else:
                failed += 1
                logger.error("[%s] FAIL %s: %s", current_tier, result.filename, result.error)
        elif event.phase == "tier_done":
            logger.info("[%s] done ok=%d failed=%d", event.tier, event.tier_ok, event.tier_failed)

    progress.save()
    logger.info("Volve load finished ok=%d failed=%d", ok, failed)
    return 1 if failed else 0


def _configure_logging() -> None:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    logger.handlers.clear()
    file_handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


if __name__ == "__main__":
    raise SystemExit(main())
