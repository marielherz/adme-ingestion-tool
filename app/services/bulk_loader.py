"""Bulk Load — dataset registry, preview, and sequential submit.

Filesystem-discovered datasets under ``app/data/datasets/*/dataset.json``.
No network in ``list_datasets``/``load_dataset``/``preview_tier`` — those
are pure file IO. ``submit_tier`` is a generator that delegates each
manifest to the existing :func:`app.services.ingestion.submit_manifest`
and yields one :class:`SubmitResult` per file.

Path safety: every resolved manifest path is asserted to live under
``app/data/`` so a malicious bring-your-own descriptor cannot
``../../../etc/passwd`` its way out.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.models.connection import ADMEConnection
from app.models.osdu import (
    DatasetDescriptor,
    DatasetTier,
    ManifestPreview,
    SubmitResult,
    WorkflowStatus,
)
from app.services.ingestion import submit_manifest
from app.services.run_history import (
    RUN_HISTORY_WRITE_ERRORS,
    record_workflow_finish,
    record_workflow_submit,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DATASETS_ROOT",
    "DATA_ROOT",
    "SUBMIT_SOURCE",
    "apply_load_prefix",
    "apply_prefix_to_body",
    "build_prefix_id_map",
    "build_reference_prefix_map",
    "inject_acl_and_legal",
    "list_datasets",
    "load_dataset",
    "make_load_prefix",
    "preview_tier",
    "submit_manifest_paths",
    "submit_tier",
]

SUBMIT_SOURCE = "bulk_load"

# ``app/data/`` is the security boundary: every resolved manifest path
# MUST live underneath it, no exceptions.
DATA_ROOT: Path = (Path(__file__).resolve().parent.parent / "data").resolve()
DATASETS_ROOT: Path = (DATA_ROOT / "datasets").resolve()

_TIER_TO_SECTION: dict[str, str] = {
    "reference-data": "ReferenceData",
    "master-data": "MasterData",
    "work-products": "Data",
}

_DATASET_CACHE: list[DatasetDescriptor] | None = None


def _clear_cache() -> None:
    """Drop the module-level dataset cache (tests + page mount)."""

    global _DATASET_CACHE
    _DATASET_CACHE = None


def _assert_under_data_root(path: Path) -> Path:
    """Return ``path.resolve()`` after asserting it lives under ``DATA_ROOT``.

    Raises ``ValueError`` if the resolved path is outside ``app/data/``.
    """
    resolved = path.resolve()
    try:
        resolved.relative_to(DATA_ROOT)
    except ValueError as exc:
        raise ValueError(
            f"Path {resolved!s} escapes the app/data/ sandbox."
        ) from exc
    return resolved


def _parse_tier(raw: Any) -> DatasetTier:
    if not isinstance(raw, dict):
        raise ValueError("tier entry must be a JSON object")
    # Default enabled to True when manifest_glob is present and no
    # explicit ``enabled`` key is given — matches the schema in
    # Satya's decision §2 where enabled tiers can omit the flag.
    manifest_glob = raw.get("manifest_glob")
    if "enabled" in raw:
        enabled = bool(raw["enabled"])
    else:
        enabled = manifest_glob is not None
    return DatasetTier(
        enabled=enabled,
        manifest_glob=(
            manifest_glob if isinstance(manifest_glob, str) else None
        ),
        description=(
            raw["description"]
            if isinstance(raw.get("description"), str)
            else None
        ),
        reason=raw["reason"] if isinstance(raw.get("reason"), str) else None,
    )


def _parse_descriptor(path: Path) -> DatasetDescriptor:
    """Parse a single ``dataset.json``. Raises ``ValueError`` on any error."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable dataset.json: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("dataset.json must be a JSON object")

    required = ("id", "display_name", "source_url", "notice_path")
    for key in required:
        if not isinstance(body.get(key), str) or not body[key]:
            raise ValueError(f"dataset.json missing string field {key!r}")

    tiers_raw = body.get("tiers")
    if not isinstance(tiers_raw, dict) or not tiers_raw:
        raise ValueError("dataset.json missing non-empty 'tiers' object")

    tiers: dict[str, DatasetTier] = {}
    for name, entry in tiers_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("tier name must be a non-empty string")
        tiers[name] = _parse_tier(entry)

    return DatasetDescriptor(
        id=body["id"],
        display_name=body["display_name"],
        source_url=body["source_url"],
        notice_path=body["notice_path"],
        tiers=tiers,
        root_dir=path.parent.resolve(),
    )


def list_datasets() -> list[DatasetDescriptor]:
    """Scan ``app/data/datasets/*/dataset.json``; sorted by display_name.

    Malformed descriptors are logged and skipped, not raised. Result is
    cached at module level; call :func:`_clear_cache` to force a re-scan.
    """
    global _DATASET_CACHE
    if _DATASET_CACHE is not None:
        return list(_DATASET_CACHE)

    found: list[DatasetDescriptor] = []
    if DATASETS_ROOT.is_dir():
        for descriptor_path in sorted(DATASETS_ROOT.glob("*/dataset.json")):
            try:
                descriptor = _parse_descriptor(descriptor_path)
            except ValueError as exc:
                logger.warning(
                    "Skipping malformed dataset descriptor %s: %s",
                    descriptor_path,
                    exc,
                )
                continue
            found.append(descriptor)

    found.sort(key=lambda d: d.display_name.lower())
    _DATASET_CACHE = found
    return list(found)


def load_dataset(dataset_id: str) -> DatasetDescriptor:
    """Return the descriptor with this ``id``. Raises ``ValueError`` if absent."""
    for descriptor in list_datasets():
        if descriptor.id == dataset_id:
            return descriptor
    raise ValueError(f"Unknown dataset id: {dataset_id!r}")


def _resolve_tier(descriptor: DatasetDescriptor, tier: str) -> DatasetTier:
    tier_descriptor = descriptor.tiers.get(tier)
    if tier_descriptor is None:
        raise ValueError(
            f"Dataset {descriptor.id!r} has no tier {tier!r}."
        )
    if not tier_descriptor.enabled:
        reason = tier_descriptor.reason or "tier disabled"
        raise ValueError(
            f"Tier {tier!r} on dataset {descriptor.id!r} is disabled: "
            f"{reason}."
        )
    if not tier_descriptor.manifest_glob:
        raise ValueError(
            f"Tier {tier!r} on dataset {descriptor.id!r} is enabled but "
            f"has no manifest_glob."
        )
    return tier_descriptor


def _resolve_manifests(
    descriptor: DatasetDescriptor,
    tier_descriptor: DatasetTier,
) -> list[Path]:
    glob = tier_descriptor.manifest_glob
    assert glob is not None  # guarded by _resolve_tier
    base = descriptor.root_dir
    # Split the glob into a "static prefix" we can resolve safely and a
    # pattern we hand to glob.glob. We resolve the prefix first and
    # check it stays under DATA_ROOT, then expand.
    glob_path = (base / glob)
    # Resolve parent (with .. parts) then re-attach the wildcard segment.
    parent = glob_path.parent
    resolved_parent = _assert_under_data_root(parent)
    pattern = glob_path.name
    matches = sorted(resolved_parent.glob(pattern))
    safe_matches: list[Path] = []
    for match in matches:
        safe_matches.append(_assert_under_data_root(match))
    return safe_matches


def preview_tier(dataset_id: str, tier: str) -> list[ManifestPreview]:
    """Return one :class:`ManifestPreview` per manifest under this tier.

    Pure: no HTTP, no token. ``kind`` and ``record_count`` come from the
    manifest body itself. Raises ``ValueError`` if the dataset or tier
    is unknown, or if the tier is disabled.
    """
    descriptor = load_dataset(dataset_id)
    tier_descriptor = _resolve_tier(descriptor, tier)
    section = _TIER_TO_SECTION.get(tier, "ReferenceData")

    previews: list[ManifestPreview] = []
    for manifest_path in _resolve_manifests(descriptor, tier_descriptor):
        kind = ""
        record_count = 0
        try:
            body = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Cannot preview manifest {manifest_path.name}: {exc}"
            ) from exc
        if not isinstance(body, dict):
            raise ValueError(
                f"Cannot preview manifest {manifest_path.name}: "
                "manifest body is not a JSON object"
            )
        kind_raw = body.get("kind")
        kind = kind_raw if isinstance(kind_raw, str) else ""
        records = body.get(section)
        if isinstance(records, list):
            record_count = len(records)
        previews.append(
            ManifestPreview(
                path=manifest_path,
                filename=manifest_path.name,
                kind=kind,
                record_count=record_count,
                record_section=section,
            )
        )
    return previews


def inject_acl_and_legal(
    manifest_body: dict[str, Any],
    *,
    section: str,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Return a deep copy of ``manifest_body`` with ACL/legal populated.

    By default only empty arrays are filled — operator-provided values
    stay intact. Pass ``overwrite=True`` to force the operator's ACL /
    legal onto every record regardless of existing content; this is
    needed for the pre-generated TNO manifests, which ship placeholder
    groups (``ownergroup@testcompany.com``) and legal tags that do not
    exist in the target partition. We mutate the copy so the caller can
    keep the parsed body for diagnostics.
    """
    out = copy.deepcopy(manifest_body)
    records = out.get(section)
    if not isinstance(records, list):
        return out
    for record in records:
        _stamp_record_acl_legal(
            record,
            acl_owners=acl_owners,
            acl_viewers=acl_viewers,
            legal_tag=legal_tag,
            overwrite=overwrite,
        )
    return out


def _stamp_record_acl_legal(
    record: Any,
    *,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
    overwrite: bool = False,
) -> None:
    """Fill (or overwrite) the ``acl`` / ``legal`` blocks on one record."""
    if not isinstance(record, dict):
        return
    acl = record.get("acl")
    if not isinstance(acl, dict):
        acl = {}
        record["acl"] = acl
    if overwrite or not acl.get("owners"):
        acl["owners"] = list(acl_owners)
    if overwrite or not acl.get("viewers"):
        acl["viewers"] = list(acl_viewers)
    legal = record.get("legal")
    if not isinstance(legal, dict):
        legal = {}
        record["legal"] = legal
    if overwrite or not legal.get("legaltags"):
        legal["legaltags"] = [legal_tag]



# Deprecated private name retained for one release while callers
# migrate to the public ``inject_acl_and_legal``. See
# decisions: kevin-bulk-ingest-contract-2026-05-19.md \u00a72.
_inject_acl_and_legal = inject_acl_and_legal


def make_load_prefix(load_date: date | None = None) -> str:
    """Return a date-based load prefix such as ``"20260630-"``.

    Each Smart Tier load needs its own prefix so the three copies live as
    independent records that age on their own clock. Defaults to today's
    UTC date.
    """
    chosen = load_date or datetime.now(UTC).date()
    return f"{chosen:%Y%m%d}-"


def _split_osdu_id(value: Any) -> tuple[str, str, str, str] | None:
    """Split an OSDU id/reference into ``(lead, entity_type, unique, rest)``.

    Recognises the ``<lead>:<entity-type>:<unique>[:<version>]`` shape where
    the entity type carries the ``--`` group marker (e.g.
    ``master-data--Well``). ``lead`` keeps the partition or ``<namespace>``
    placeholder token; ``rest`` is the trailing version portion (including
    its leading colon) or ``""``. Returns ``None`` for anything that is not
    id-shaped (plain strings, schema ``kind`` values, etc.).
    """
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) < 3:
        return None
    lead, entity_type, unique = parts[0], parts[1], parts[2]
    if "--" not in entity_type or not unique:
        return None
    rest = "" if len(parts) == 3 else ":" + ":".join(parts[3:])
    return lead, entity_type, unique, rest


def build_reference_prefix_map(
    node: Any,
    *,
    prefix: str,
    entity_prefix: str,
) -> dict[tuple[str, str], str]:
    """Map ``(entity_type, unique) -> prefixed_unique`` for every id-shaped
    *reference* anywhere in ``node`` whose entity type starts with
    ``entity_prefix``.

    Unlike :func:`build_prefix_id_map` (which reads record ``id`` fields in a
    known section), this walks arbitrary nested structures and keys off the
    references themselves — used to rewrite the master-data references inside
    a work-product manifest so an independent (prefixed) load links to that
    load's prefixed master-data. A blank ``prefix`` yields an empty map.
    """
    out: dict[tuple[str, str], str] = {}
    cleaned = prefix.strip()
    if not cleaned:
        return out

    def _walk(current: Any) -> None:
        if isinstance(current, dict):
            for value in current.values():
                _walk(value)
        elif isinstance(current, list):
            for value in current:
                _walk(value)
        elif isinstance(current, str):
            parsed = _split_osdu_id(current)
            if parsed is not None:
                _lead, entity_type, unique, _rest = parsed
                if entity_type.startswith(entity_prefix):
                    out[(entity_type, unique)] = f"{cleaned}{unique}"

    _walk(node)
    return out


def build_prefix_id_map(
    manifest_bodies: Iterable[dict[str, Any]],
    *,
    section: str,
    prefix: str,
) -> dict[tuple[str, str], str]:
    """Map ``(entity_type, unique_id) -> prefixed_unique_id`` for every
    record in ``section`` across the given manifest bodies.

    Keyed on ``(entity_type, unique_id)`` rather than the full id so that a
    record id (which uses the literal ``osdu:`` token) and the references
    that point at it (which use the ``<namespace>:`` placeholder and a
    trailing version colon) both resolve to the same entry. An empty or
    blank ``prefix`` yields an empty map (no-op).
    """
    id_map: dict[tuple[str, str], str] = {}
    cleaned = prefix.strip()
    if not cleaned:
        return id_map
    for body in manifest_bodies:
        records = body.get(section)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            parsed = _split_osdu_id(record.get("id"))
            if parsed is None:
                continue
            _lead, entity_type, unique, _rest = parsed
            id_map[(entity_type, unique)] = f"{cleaned}{unique}"
    return id_map


def _rewrite_id_string(
    value: str, id_map: dict[tuple[str, str], str]
) -> str:
    parsed = _split_osdu_id(value)
    if parsed is None:
        return value
    lead, entity_type, unique, rest = parsed
    new_unique = id_map.get((entity_type, unique))
    if new_unique is None:
        return value
    return f"{lead}:{entity_type}:{new_unique}{rest}"


def _rewrite_node(node: Any, id_map: dict[tuple[str, str], str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                node[key] = _rewrite_id_string(value, id_map)
            else:
                _rewrite_node(value, id_map)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                node[index] = _rewrite_id_string(value, id_map)
            else:
                _rewrite_node(value, id_map)


def apply_prefix_to_body(
    manifest_body: dict[str, Any],
    id_map: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """Return a deep copy of ``manifest_body`` with every id-shaped string
    whose ``(entity_type, unique)`` is in ``id_map`` rewritten so its
    unique-id portion carries the load prefix.

    The partition / ``<namespace>`` token and any trailing version are left
    untouched, and references to records outside this load (e.g. shared
    reference-data) are ignored — only links to records that are part of the
    same prefixed load are rewritten, preserving referential integrity.
    An empty ``id_map`` returns an untouched deep copy.
    """
    out = copy.deepcopy(manifest_body)
    if id_map:
        _rewrite_node(out, id_map)
    return out


def apply_load_prefix(
    manifest_bodies: Sequence[dict[str, Any]],
    *,
    section: str,
    prefix: str,
) -> list[dict[str, Any]]:
    """Rewrite a set of manifest bodies into an independent, internally
    consistent copy under ``prefix``.

    Builds the cross-manifest id map first so references that span files
    (e.g. a ``Wellbore`` pointing at a ``Well`` in another manifest) stay
    linked. A blank ``prefix`` returns deep copies unchanged.
    """
    id_map = build_prefix_id_map(
        manifest_bodies, section=section, prefix=prefix
    )
    return [apply_prefix_to_body(body, id_map) for body in manifest_bodies]


def _extract_record_id(result: Any) -> str | None:
    raw = getattr(result, "raw_response", None)
    if isinstance(raw, dict):
        candidate = raw.get("recordId") or raw.get("record_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _format_utc_iso(value: datetime) -> str:
    """Format a UTC ``datetime`` as the run-history ISO 8601 shape."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _top_level_kind(manifest_body: dict[str, Any]) -> str | None:
    kind = manifest_body.get("kind")
    return kind if isinstance(kind, str) and kind else None


def _record_submit_history(
    workflow_result: Any,
    *,
    manifest_body: dict[str, Any],
    submitted_at: datetime,
    data_partition_id: str,
) -> None:
    run_id = getattr(workflow_result, "run_id", None)
    if not isinstance(run_id, str) or not run_id:
        return

    try:
        record_workflow_submit(
            run_id=run_id,
            submitted_at=_format_utc_iso(submitted_at),
            kind=_top_level_kind(manifest_body),
            correlation_id=getattr(workflow_result, "correlation_id", None),
            submit_source=SUBMIT_SOURCE,
            data_partition_id=data_partition_id,
        )
    except RUN_HISTORY_WRITE_ERRORS:
        logger.exception("record_workflow_submit failed for bulk run %s", run_id)

    workflow_status = getattr(workflow_result, "status", None)
    if workflow_status not in (WorkflowStatus.FINISHED, WorkflowStatus.FAILED):
        return

    error_message = getattr(workflow_result, "error_message", None)
    latency_ms_raw = getattr(workflow_result, "latency_ms", 0)
    try:
        latency_ms = int(float(latency_ms_raw))
    except (TypeError, ValueError):
        latency_ms = 0

    try:
        record_workflow_finish(
            run_id=run_id,
            finished_at=_format_utc_iso(datetime.now(UTC)),
            status=workflow_status,
            latency_ms=latency_ms,
            error_message=error_message if isinstance(error_message, str) else None,
        )
    except RUN_HISTORY_WRITE_ERRORS:
        logger.exception("record_workflow_finish failed for bulk run %s", run_id)


def submit_tier(
    dataset_id: str,
    tier: str,
    *,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
    data_partition_id: str,
    connection: ADMEConnection,
    token: str,
    load_prefix: str = "",
    overwrite_acl_legal: bool = False,
    progress_callback: Callable[[SubmitResult], None] | None = None,
) -> Iterator[SubmitResult]:
    """Yield one :class:`SubmitResult` per manifest in this tier.

    Sequential — one submit at a time. A failure on one manifest yields
    an error result and the loop continues to the next file (v1 has no
    abort-on-error policy at the service layer; the page can stop
    consuming the iterator).

    When ``load_prefix`` is set, every record id (and the intra-load
    references that point at it) is rewritten so its unique-id portion
    carries the prefix, making this submission an independent copy that
    ages on its own tier clock. References across manifests in the tier
    stay linked because the prefix map is built from all files first.
    """
    descriptor = load_dataset(dataset_id)
    tier_descriptor = _resolve_tier(descriptor, tier)
    section = _TIER_TO_SECTION.get(tier, "ReferenceData")
    manifests = _resolve_manifests(descriptor, tier_descriptor)

    yield from submit_manifest_paths(
        manifests,
        section=section,
        acl_owners=acl_owners,
        acl_viewers=acl_viewers,
        legal_tag=legal_tag,
        data_partition_id=data_partition_id,
        connection=connection,
        token=token,
        load_prefix=load_prefix,
        overwrite_acl_legal=overwrite_acl_legal,
        progress_callback=progress_callback,
    )


def submit_manifest_paths(
    manifest_paths: Sequence[Path],
    *,
    section: str,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
    data_partition_id: str,
    connection: ADMEConnection,
    token: str,
    load_prefix: str = "",
    overwrite_acl_legal: bool = False,
    progress_callback: Callable[[SubmitResult], None] | None = None,
) -> Iterator[SubmitResult]:
    """Yield one :class:`SubmitResult` per explicit manifest path.

    The shared engine behind :func:`submit_tier` and the external
    downloaded-dataset loader: builds the cross-file prefix map (when
    ``load_prefix`` is set), stamps ACL/legal (``overwrite_acl_legal`` for
    pre-generated manifests carrying placeholder groups), and submits each
    list-section manifest. Sequential; a failure yields an error result and
    the loop continues.
    """
    # Pre-pass: build the cross-manifest prefix map. Read errors here are
    # ignored on purpose — the main loop re-reads each file and surfaces
    # any failure as an error result for that manifest.
    id_map: dict[tuple[str, str], str] = {}
    if load_prefix.strip():
        scanned: list[dict[str, Any]] = []
        for manifest_path in manifest_paths:
            try:
                scanned_body = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(scanned_body, dict):
                scanned.append(scanned_body)
        id_map = build_prefix_id_map(
            scanned, section=section, prefix=load_prefix
        )

    for manifest_path in manifest_paths:
        submitted_at = datetime.now(UTC)
        run_id: str | None = None
        record_id: str | None = None
        status = "error"
        error: str | None = None

        try:
            body = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(body, dict):
                raise ValueError("manifest body is not a JSON object")
            if id_map:
                body = apply_prefix_to_body(body, id_map)
            shaped = inject_acl_and_legal(
                body,
                section=section,
                acl_owners=acl_owners,
                acl_viewers=acl_viewers,
                legal_tag=legal_tag,
                overwrite=overwrite_acl_legal,
            )
            payload = {
                "executionContext": {
                    "Payload": {
                        "AppKey": "adme-ingestion-tool",
                        "data-partition-id": data_partition_id,
                    },
                    "manifest": shaped,
                },
            }

            workflow_result = submit_manifest(connection, token, payload)
            if getattr(workflow_result, "ok", False):
                status = "success"
                run_id = getattr(workflow_result, "run_id", None)
                record_id = _extract_record_id(workflow_result)
                _record_submit_history(
                    workflow_result,
                    manifest_body=shaped,
                    submitted_at=submitted_at,
                    data_partition_id=data_partition_id,
                )
            else:
                error = (
                    getattr(workflow_result, "error_message", None)
                    or "submit_manifest returned ok=False"
                )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            error = str(exc) or exc.__class__.__name__
            logger.warning(
                "Manifest %s failed to submit: %s", manifest_path, exc
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
                logger.exception("bulk_loader progress_callback failed")
        yield result
