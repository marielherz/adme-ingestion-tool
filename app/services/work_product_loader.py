"""Work-product loader — upload blobs then submit pre-generated manifests.

The TNO download ships fully-generated work-product manifests under
``TNO/provided/work-products/**`` shaped as::

    {"kind": "osdu:wks:Manifest:1.0.0",
     "Data": {"WorkProduct": {...},
              "WorkProductComponents": [{...}],
              "Datasets": [{"kind": "osdu:wks:dataset--File.Generic:1.0.0",
                            "data": {"DatasetProperties": {"FileSourceInfo": {
                              "FileSource": "s3://.../well-logs/1013_...las"}}}}]}}

The ``surrogate-key:`` links between WorkProduct / WorkProductComponents /
Datasets are resolved server-side by the Manifest Ingestion DAG, so every
submit mints fresh ids — work-products are therefore independent per load
with no id prefix required. Only the real file blob has to be staged
first: for each Dataset we upload the local file to Azure Blob (reusing
the File Service ``uploadURL`` + ``PUT`` primitives), then swap the
placeholder ``FileSource`` for the returned staging token before submit.

We register each Dataset through the File Service ``POST /files/metadata``
call (``post_file_metadata``) rather than letting the DAG create the
File.Generic record from the manifest's ``Datasets`` section. This is
**required for the blob to be retrievable**: only the File Service call
promotes the staged blob out of the transient landing zone into the
persistent area. Registering datasets via the DAG leaves the blob in
staging where it is purged by retention, so ``downloadURL`` later 404s
even though the metadata record exists. We therefore replace each
WorkProductComponent's ``surrogate-key:file-*`` reference with the real
File record id the File Service returns, and drop the ``Data.Datasets``
section so the DAG only creates the WorkProduct + WorkProductComponents.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models.connection import ADMEConnection
from app.models.osdu import SubmitResult
from app.services.bulk_loader import (
    SUBMIT_SOURCE,
    _resolve_active_token,
    _stamp_record_acl_legal,
    apply_prefix_to_body,
    build_reference_prefix_map,
    substitute_partition,
)
from app.services.file_uploader import (
    UPLOAD_BYTES_TIMEOUT_SECONDS,
    guess_content_type,
)
from app.services.files import (
    FILES_BLOCK_UPLOAD_THRESHOLD_BYTES,
    get_upload_url,
    post_file_metadata,
    upload_file_blocks,
    upload_file_bytes,
)
from app.services.ingestion import submit_manifest

logger = logging.getLogger(__name__)

WORK_PRODUCT_SUBMIT_SOURCE = SUBMIT_SOURCE

# The s3 preload path's parent directory usually matches a ``datasets/``
# subfolder verbatim; these are the ones that don't.
DATASET_DIR_ALIASES: dict[str, str] = {
    "USGS_docs": "documents",
}

_VERSION_SUFFIX = re.compile(r"_\d+_\d+_\d+$")

__all__ = [
    "DATASET_DIR_ALIASES",
    "WORK_PRODUCT_SUBMIT_SOURCE",
    "apply_uploaded_file_sources",
    "collect_file_sources",
    "relink_datasets_to_records",
    "resolve_local_file",
    "stamp_work_product_acl_legal",
    "submit_work_products",
]


def resolve_local_file(
    file_source: str,
    *,
    datasets_root: Path,
    aliases: Mapping[str, str] = DATASET_DIR_ALIASES,
) -> Path | None:
    """Resolve a manifest ``FileSource`` to a local file under ``datasets_root``.

    Uses the *parent directory* of the (s3-style) source path, not just the
    basename — ``markers/1000.csv`` and ``trajectories/1000.csv`` collide on
    basename alone. Applies ``aliases`` (e.g. ``USGS_docs`` -> ``documents``)
    and falls back to a version-suffix-stripped directory
    (``well-logs_1_1_0`` -> ``well-logs``). Returns ``None`` when no
    candidate exists.
    """
    normalized = file_source.replace("\\", "/").rstrip("/")
    parts = [p for p in normalized.split("/") if p]
    if not parts:
        return None
    name = parts[-1]
    parent = parts[-2] if len(parts) >= 2 else ""

    candidates: list[str] = []
    mapped = aliases.get(parent, parent)
    if mapped:
        candidates.append(mapped)
        stripped = _VERSION_SUFFIX.sub("", mapped)
        if stripped and stripped != mapped:
            candidates.append(aliases.get(stripped, stripped))

    seen: set[str] = set()
    for sub in candidates:
        if sub in seen:
            continue
        seen.add(sub)
        candidate = datasets_root / sub / name
        if candidate.is_file():
            return candidate

    # Volve seismic sources include an extra category directory, e.g.
    # ``.../seismic/st0299/file.segy``. The downloader preserves that layout,
    # so also try the final two source path components below datasets_root.
    if len(parts) >= 3:
        nested = datasets_root.joinpath(*parts[-3:])
        if nested.is_file():
            return nested
        seismic_nested = datasets_root / "seismic" / Path(*parts[-3:])
        if seismic_nested.is_file():
            return seismic_nested
    return None


def stamp_work_product_acl_legal(
    manifest_body: dict[str, Any],
    *,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
    overwrite: bool = True,
) -> None:
    """Stamp ACL/legal onto the WorkProduct, its components, and datasets.

    Mutates ``manifest_body`` in place. ``overwrite`` defaults to ``True``
    because the TNO manifests ship placeholder groups/legal tags that must
    be replaced with the operator's real values.
    """
    data = manifest_body.get("Data")
    if not isinstance(data, dict):
        return
    work_product = data.get("WorkProduct")
    if isinstance(work_product, dict):
        _stamp_record_acl_legal(
            work_product,
            acl_owners=acl_owners,
            acl_viewers=acl_viewers,
            legal_tag=legal_tag,
            overwrite=overwrite,
        )
    for key in ("WorkProductComponents", "Datasets"):
        records = data.get(key)
        if isinstance(records, list):
            for record in records:
                _stamp_record_acl_legal(
                    record,
                    acl_owners=acl_owners,
                    acl_viewers=acl_viewers,
                    legal_tag=legal_tag,
                    overwrite=overwrite,
                )


def _dataset_records(manifest_body: dict[str, Any]) -> list[dict[str, Any]]:
    data = manifest_body.get("Data")
    if not isinstance(data, dict):
        return []
    datasets = data.get("Datasets")
    if not isinstance(datasets, list):
        return []
    return [ds for ds in datasets if isinstance(ds, dict)]


def collect_file_sources(manifest_body: dict[str, Any]) -> list[str]:
    """Return each Dataset's declared ``FileSource`` (s3 preload path)."""
    sources: list[str] = []
    for dataset in _dataset_records(manifest_body):
        info = (
            dataset.get("data", {})
            .get("DatasetProperties", {})
            .get("FileSourceInfo", {})
        )
        source = info.get("FileSource") if isinstance(info, dict) else None
        sources.append(source if isinstance(source, str) else "")
    return sources


def apply_uploaded_file_sources(
    manifest_body: dict[str, Any],
    file_sources: Sequence[str],
) -> None:
    """Replace each Dataset's ``FileSource`` with the staged blob token.

    ``file_sources`` is positional, aligned with :func:`collect_file_sources`.
    The now-meaningless ``PreloadFilePath`` is dropped so the record does not
    keep the original s3 pointer.
    """
    datasets = _dataset_records(manifest_body)
    for dataset, token in zip(datasets, file_sources, strict=False):
        info = (
            dataset.get("data", {})
            .get("DatasetProperties", {})
            .get("FileSourceInfo")
        )
        if not isinstance(info, dict):
            continue
        info["FileSource"] = token
        info.pop("PreloadFilePath", None)


def _versioned_ref(record_id: str) -> str:
    """Return an OSDU "latest version" reference for ``record_id``.

    OSDU references end with a trailing ``:`` (the empty version marker),
    e.g. ``opendes:dataset--File.Generic:<guid>:``. Idempotent.
    """
    return record_id if record_id.endswith(":") else f"{record_id}:"


def _register_dataset_files(
    connection: ADMEConnection,
    token: str,
    manifest_body: dict[str, Any],
    *,
    datasets_root: Path,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
) -> tuple[dict[str, str], str | None]:
    """Upload + register every Dataset via the File Service.

    For each Dataset: upload the local blob, then ``post_file_metadata`` so
    the File Service promotes the blob to the persistent area and mints a
    real File.Generic record id. Returns ``({surrogate_id: real_id}, error)``
    where ``surrogate_id`` is the Dataset's ``id`` (e.g. ``surrogate-key:file-1``).
    Stops at the first failure and returns the partial map plus the error.
    """
    owner = acl_owners[0] if acl_owners else ""
    viewer = acl_viewers[0] if acl_viewers else ""
    id_map: dict[str, str] = {}
    for dataset in _dataset_records(manifest_body):
        surrogate = dataset.get("id")
        data = dataset.get("data") if isinstance(dataset.get("data"), dict) else {}
        info = data.get("DatasetProperties", {}).get("FileSourceInfo", {})
        source = info.get("FileSource") if isinstance(info, dict) else None
        if not source:
            return id_map, "Dataset is missing a FileSource path."
        local = resolve_local_file(source, datasets_root=datasets_root)
        if local is None:
            return id_map, f"Local file not found for {source!r}."
        try:
            file_size = local.stat().st_size
        except OSError as exc:
            return id_map, f"Cannot read {local.name}: {exc}"
        if file_size <= 0:
            return id_map, f"{local.name} is empty; nothing to upload."

        url_result = get_upload_url(connection, token)
        if (
            not url_result.ok
            or not url_result.signed_url
            or not url_result.file_source
        ):
            return id_map, (
                url_result.error_message or "Failed to allocate upload URL."
            )
        bytes_result = _upload_local_file(url_result.signed_url, local, file_size)
        if not bytes_result.ok:
            return id_map, (
                bytes_result.error_message or f"Failed to upload {local.name}."
            )

        display_name = (
            info.get("Name") if isinstance(info, dict) else None
        ) or local.name
        extra_data = {
            key: value
            for key, value in data.items()
            if key not in ("Name", "Description", "DatasetProperties")
        }
        meta = post_file_metadata(
            connection,
            token,
            file_source=url_result.file_source,
            file_id=url_result.file_id or "",
            display_name=display_name,
            description=str(data.get("Description", "")),
            legal_tag=legal_tag,
            acl_owners=owner,
            acl_viewers=viewer,
            extra_data=extra_data,
        )
        if not meta.ok or not meta.record_id:
            return id_map, (
                meta.error_message or f"Failed to register {local.name}."
            )
        if isinstance(surrogate, str) and surrogate:
            id_map[surrogate] = meta.record_id
    return id_map, None


def _upload_local_file(signed_url: str, local: Path, file_size: int):
    """Upload ``local`` through the right Azure Blob primitive for its size."""
    content_type = guess_content_type(local)
    if file_size >= FILES_BLOCK_UPLOAD_THRESHOLD_BYTES:
        return upload_file_blocks(
            signed_url,
            local,
            content_type=content_type,
        )
    return upload_file_bytes(
        signed_url,
        local.read_bytes(),
        content_type=content_type,
        timeout=UPLOAD_BYTES_TIMEOUT_SECONDS,
    )


def relink_datasets_to_records(
    manifest_body: dict[str, Any],
    id_map: Mapping[str, str],
) -> None:
    """Point WorkProductComponents at real File ids and drop ``Data.Datasets``.

    Rewrites each ``WorkProductComponent.data.Datasets`` surrogate reference
    that appears in ``id_map`` to the real (version-decorated) File record
    id, then removes the ``Data.Datasets`` section so the DAG does not
    recreate an un-promoted duplicate File.Generic record. Mutates in place.
    """
    data = manifest_body.get("Data")
    if not isinstance(data, dict):
        return
    components = data.get("WorkProductComponents")
    if isinstance(components, list):
        for wpc in components:
            if not isinstance(wpc, dict):
                continue
            wpc_data = wpc.get("data")
            if not isinstance(wpc_data, dict):
                continue
            datasets = wpc_data.get("Datasets")
            if isinstance(datasets, list):
                wpc_data["Datasets"] = [
                    _versioned_ref(id_map[ref])
                    if isinstance(ref, str) and ref in id_map
                    else ref
                    for ref in datasets
                ]
    data.pop("Datasets", None)


def submit_work_products(
    manifest_paths: Sequence[Path],
    *,
    datasets_root: Path,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
    data_partition_id: str,
    connection: ADMEConnection,
    token: str,
    load_prefix: str = "",
    token_provider: Callable[[], str] | None = None,
    progress_callback: Callable[[SubmitResult], None] | None = None,
) -> Iterator[SubmitResult]:
    """Yield one :class:`SubmitResult` per work-product manifest.

    Per manifest: stage every referenced blob, swap the FileSource tokens,
    stamp ACL/legal (overwrite), optionally rewrite master-data references
    for an independent load (``load_prefix`` — must match the prefix the
    master-data tier was loaded under), then submit. Sequential; a failure
    yields an error result and the loop continues to the next manifest.

    When ``token_provider`` is given it is called once per manifest to obtain
    a current token (refreshing near expiry) so a long blob-upload load does
    not die when a single token expires; otherwise the fixed ``token`` is
    used.
    """
    for manifest_path in manifest_paths:
        submitted_at = datetime.now(UTC)
        run_id: str | None = None
        record_id: str | None = None
        status = "error"
        error: str | None = None

        try:
            active_token = _resolve_active_token(token, token_provider)
            body = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(body, dict):
                raise ValueError("manifest body is not a JSON object")

            id_map, stage_error = _register_dataset_files(
                connection,
                active_token,
                body,
                datasets_root=datasets_root,
                acl_owners=acl_owners,
                acl_viewers=acl_viewers,
                legal_tag=legal_tag,
            )
            if stage_error is not None:
                raise ValueError(stage_error)

            relink_datasets_to_records(body, id_map)
            stamp_work_product_acl_legal(
                body,
                acl_owners=acl_owners,
                acl_viewers=acl_viewers,
                legal_tag=legal_tag,
                overwrite=True,
            )
            if load_prefix.strip():
                master_map = build_reference_prefix_map(
                    body,
                    prefix=load_prefix,
                    entity_prefix="master-data--",
                )
                if master_map:
                    body = apply_prefix_to_body(body, master_map)

            body = substitute_partition(body, data_partition_id)

            payload = {
                "executionContext": {
                    "Payload": {
                        "AppKey": "adme-ingestion-tool",
                        "data-partition-id": data_partition_id,
                    },
                    "manifest": body,
                },
            }
            workflow_result = submit_manifest(connection, active_token, payload)
            if getattr(workflow_result, "ok", False):
                status = "success"
                run_id = getattr(workflow_result, "run_id", None)
            else:
                error = (
                    getattr(workflow_result, "error_message", None)
                    or "submit_manifest returned ok=False"
                )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            error = str(exc) or exc.__class__.__name__
            logger.warning(
                "Work-product %s failed to submit: %s", manifest_path, exc
            )

        result = SubmitResult(
            manifest_path=manifest_path,
            filename=manifest_path.name,
            status=status,
            run_id=run_id,
            record_id=record_id,
            error=error,
            submitted_at=submitted_at,
        )
        if progress_callback is not None:
            try:
                progress_callback(result)
            except Exception:  # pragma: no cover - UI callback never fatal
                logger.exception(
                    "submit_work_products progress_callback failed"
                )
        yield result
