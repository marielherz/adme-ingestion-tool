"""Load one small Volve SEG-Y file through the ADME seismic DMS path.

Default file is a 12.6 MB 2D SEG-Y line, intentionally chosen before testing
large 3D volumes. The script uses the File service metadata flow so the blob is
promoted to persistent storage and remains downloadable.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from urllib.parse import quote

import requests

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.connection import ADMEConnection, AuthMethod  # noqa: E402
from app.services.auth import acquire_cli_token  # noqa: E402
from app.services.work_product_loader import submit_work_products  # noqa: E402

ENDPOINT = "https://marielsmrttier.energy.azure.com"
TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
CLIENT_ID = "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2"
DATA_PARTITION_ID = "opendes"
TOKEN_SCOPE = "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2/.default"
LEGAL_TAG = "opendes-referencedata-legal"
ACL_OWNER = "data.default.owners@opendes.dataservices.energy"
ACL_VIEWER = "data.default.viewers@opendes.dataservices.energy"
S3_SOURCE = "s3://osdu-seismic-test-data/volve/seismic/st0299/ST0299-05002+MIG_FIN.segy"
DEFAULT_ROOT = Path.home() / "osdu-data" / "volve"

logger = logging.getLogger("volve_seismic_smoke")


def main() -> int:
    parser = argparse.ArgumentParser(description="Load one Volve SEG-Y smoke test")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--source", default=S3_SOURCE)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    local = _local_path_for_source(root / "datasets", args.source)
    local.parent.mkdir(parents=True, exist_ok=True)
    if args.force or not local.is_file():
        logger.info("Downloading %s", args.source)
        _download_s3(args.source, local)
    logger.info("Using %s (%d bytes)", local, local.stat().st_size)

    manifest_dir = root / "generated-json" / "seismic-smoke"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "st0299-05002-seismic-trace.json"
    manifest_path.write_text(
        json.dumps(_manifest(args.source), indent=2), encoding="utf-8"
    )

    connection = ADMEConnection(
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        data_partition_id=DATA_PARTITION_ID,
        token_scope=TOKEN_SCOPE,
        auth_method=AuthMethod.USER_IMPERSONATION,
    )
    token = acquire_cli_token()
    results = list(
        submit_work_products(
            [manifest_path],
            datasets_root=root / "datasets",
            acl_owners=[ACL_OWNER],
            acl_viewers=[ACL_VIEWER],
            legal_tag=LEGAL_TAG,
            data_partition_id=DATA_PARTITION_ID,
            connection=connection,
            token=token,
        )
    )
    result = results[0]
    print(
        json.dumps(
            {
                "status": result.status,
                "run_id": result.run_id,
                "error": result.error,
                "manifest": str(manifest_path),
                "local_file": str(local),
                "bytes": local.stat().st_size,
            },
            indent=2,
        )
    )
    return 0 if result.status == "success" else 1


def _download_s3(source: str, target: Path) -> None:
    parts = source.removeprefix("s3://").split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid S3 source: {source}")
    url = f"https://{parts[0]}.s3.amazonaws.com/{quote(parts[1], safe='/')}"
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)


def _local_path_for_source(datasets_root: Path, source: str) -> Path:
    """Preserve the final S3 directory structure under ``datasets``."""
    object_path = source.removeprefix("s3://").split("/", 1)[-1]
    parts = [part for part in object_path.split("/") if part]
    if parts and parts[0].lower() == "volve":
        parts = parts[1:]
    if not parts:
        raise ValueError(f"Could not derive local path from {source}")
    return datasets_root.joinpath(*parts)


def _record(kind: str, record_id: str, name: str, data: dict) -> dict:
    return {
        "id": f"surrogate-key:{record_id}",
        "kind": kind,
        "acl": {"owners": [ACL_OWNER], "viewers": [ACL_VIEWER]},
        "legal": {
            "legaltags": [LEGAL_TAG],
            "otherRelevantDataCountries": ["US"],
            "status": "compliant",
        },
        "data": {"Name": name, "Source": "Volve", **data},
    }


def _manifest(source: str) -> dict:
    name = Path(source).name
    work_product = _record(
        "osdu:wks:work-product--WorkProduct:1.0.0",
        "wp-volve-seismic-st0299-05002",
        name,
        {
            "ExistenceKind": "Prototype",
            "IsDiscoverable": True,
            "IsExtendedLoad": True,
            "Components": ["surrogate-key:wpc-volve-seismic-st0299-05002"],
        },
    )
    component = _record(
        "osdu:wks:work-product-component--SeismicTraceData:1.0.0",
        "wpc-volve-seismic-st0299-05002",
        name,
        {
            "ExistenceKind": "Prototype",
            "IsDiscoverable": True,
            "IsExtendedLoad": True,
            "Seismic2DName": "ST0299-05002",
            "SeismicTraceDataDimensionalityTypeID": (
                "opendes:reference-data--SeismicTraceDataDimensionalityType:2D:"
            ),
            "SeismicDomainTypeID": "opendes:reference-data--SeismicDomainType:Time:",
            "Datasets": ["surrogate-key:file-volve-seismic-st0299-05002"],
        },
    )
    dataset = _record(
        "osdu:wks:dataset--File.Generic:1.0.0",
        "file-volve-seismic-st0299-05002",
        name,
        {
            "DatasetProperties": {
                "FileSourceInfo": {
                    "FileSource": source,
                    "Name": name,
                }
            },
            "ResourceSecurityClassification": "Public",
        },
    )
    return {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {
            "WorkProduct": work_product,
            "WorkProductComponents": [component],
            "Datasets": [dataset],
        },
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
