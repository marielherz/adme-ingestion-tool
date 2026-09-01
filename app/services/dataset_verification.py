"""Verify & Repair a loaded dataset — counts, bulk-blob checks, and repair.

This service generalizes the manual reconciliation a TNO/Volve load needs
after the fact:

* **Counts** — expected (from the on-disk manifests) vs actual (from Search)
  per record kind, so an operator can see at a glance what landed.
* **Bulk check** — samples ``dataset--File.Generic`` records and confirms
  their blobs are actually downloadable (a metadata record can exist while
  its blob was never promoted out of staging — the failure the fixed
  work-product loader guards against).
* **Diff** — for a work-product part, which expected items are *missing*
  (submit succeeded but the async DAG never persisted the WPC) and which are
  *duplicated* (a resumed load re-minted a fresh copy), matched by
  ``data.Name``.
* **Repair** — re-submit the missing manifests and delete the duplicate
  WorkProductComponent (plus its File datasets and parent WorkProduct).

Pure orchestration over existing services (``search``, ``files``,
``ingestion``, ``work_product_loader``, ``downloaded_dataset``); every
dependency is imported into this namespace so tests can monkeypatch it. No
Streamlit, no wall-clock, results are frozen dataclasses.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.models.connection import ADMEConnection
from app.models.osdu import BlobProbeResult, RecordDeleteResult, SubmitResult
from app.services.downloaded_dataset import DownloadedPart, list_part_manifests
from app.services.files import check_file_blob
from app.services.ingestion import delete_record
from app.services.search import export_all_records, get_record, search_records
from app.services.work_product_loader import submit_work_products

__all__ = [
    "FILE_GENERIC_KIND",
    "KindCount",
    "PartDiff",
    "count_kind",
    "count_kinds",
    "diff_part",
    "index_part_manifests",
    "present_wpc_records",
    "read_wpc_kind_and_name",
    "repair_duplicates",
    "repair_missing",
    "sample_bulk",
]

FILE_GENERIC_KIND = "osdu:wks:dataset--File.Generic:*"
WORK_PRODUCT_KIND = "osdu:wks:work-product--WorkProduct:*"

# Elasticsearch caps totalCount at this ceiling; counts at/above it are a
# lower bound, not exact.
SEARCH_TOTAL_CEILING = 10_000


@dataclass(frozen=True, slots=True)
class KindCount:
    """Expected-vs-actual record count for one kind."""

    label: str
    kind: str
    actual: int
    expected: int | None = None
    capped: bool = False  # actual hit the Search ceiling (lower bound only)

    @property
    def ok(self) -> bool:
        return self.expected is None or self.actual == self.expected

    @property
    def delta(self) -> int | None:
        return None if self.expected is None else self.actual - self.expected


@dataclass(frozen=True, slots=True)
class PartDiff:
    """Reconciliation of one work-product part vs the instance."""

    part_key: str
    wpc_kind: str
    expected: int
    present_records: int
    missing_names: tuple[str, ...] = ()
    duplicate_extra_ids: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.missing_names and not self.duplicate_extra_ids

    @property
    def unique_present(self) -> int:
        return self.present_records - len(self.duplicate_extra_ids)


# ---------------------------------------------------------------------------
# Manifest side (pure filesystem)
# ---------------------------------------------------------------------------


def read_wpc_kind_and_name(manifest_path: Path) -> tuple[str, str]:
    """Return ``(wpc_kind, wpc_name)`` from a work-product manifest.

    Reads the first ``WorkProductComponents`` entry's ``kind`` and
    ``data.Name``. Returns ``("", "")`` when the manifest is not shaped like
    a work-product manifest.
    """
    try:
        body = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    data = body.get("Data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return "", ""
    components = data.get("WorkProductComponents")
    if not isinstance(components, list) or not components:
        return "", ""
    first = components[0] if isinstance(components[0], dict) else {}
    kind = first.get("kind") if isinstance(first.get("kind"), str) else ""
    name = ""
    cdata = first.get("data")
    if isinstance(cdata, dict) and isinstance(cdata.get("Name"), str):
        name = cdata["Name"]
    return kind, name


def index_part_manifests(part: DownloadedPart) -> tuple[str, dict[str, Path]]:
    """Return ``(wpc_kind, {wpc_name: manifest_path})`` for a WP part.

    ``wpc_kind`` is taken from the first manifest (all manifests in a part
    share it). Names with no value are skipped.
    """
    name_to_path: dict[str, Path] = {}
    wpc_kind = ""
    for path in list_part_manifests(part):
        kind, name = read_wpc_kind_and_name(path)
        if kind and not wpc_kind:
            wpc_kind = kind
        if name:
            name_to_path[name] = path
    return wpc_kind, name_to_path


def _kind_wildcard(wpc_kind: str) -> str:
    """Turn a concrete kind (``...:1.0.0``) into a version wildcard (``...:*``)."""
    if not wpc_kind:
        return wpc_kind
    head, _, _ = wpc_kind.rpartition(":")
    return f"{head}:*" if head else wpc_kind


# ---------------------------------------------------------------------------
# Instance side (Search / Storage)
# ---------------------------------------------------------------------------


def count_kind(connection: ADMEConnection, token: str, kind: str) -> int:
    """Actual ``totalCount`` for ``kind`` (0 on error; may be ceiling-capped)."""
    res = search_records(connection, token, kind=kind, limit=1)
    return res.total_count or 0 if res.ok else 0


def count_kinds(
    connection: ADMEConnection,
    token: str,
    specs: Sequence[tuple[str, str, int | None]],
) -> list[KindCount]:
    """Build a :class:`KindCount` row per ``(label, kind, expected)`` spec."""
    rows: list[KindCount] = []
    for label, kind, expected in specs:
        actual = count_kind(connection, token, kind)
        rows.append(
            KindCount(
                label=label,
                kind=kind,
                actual=actual,
                expected=expected,
                capped=actual >= SEARCH_TOTAL_CEILING,
            )
        )
    return rows


def present_wpc_records(
    connection: ADMEConnection,
    token: str,
    wpc_kind: str,
) -> dict[str, list[tuple[str, str]]]:
    """Cursor-scroll ``wpc_kind`` and group record ids by ``data.Name``.

    Returns ``{name: [(record_id, create_time), ...]}``. Handles the Search
    10k ceiling via cursor paging. ``data.Name`` is projected (it comes back
    nested as ``source['data']['Name']``).
    """
    grouped: dict[str, list[tuple[str, str]]] = {}
    kind = _kind_wildcard(wpc_kind)
    for page in export_all_records(
        connection, token, kind=kind, limit=1000,
        returned_fields=("id", "kind", "createTime", "data.Name"),
    ):
        if not page.ok:
            break
        for rec in page.records:
            name = (rec.source.get("data") or {}).get("Name") or ""
            grouped.setdefault(name, []).append((rec.id, rec.create_time or ""))
    return grouped


def diff_part(
    connection: ADMEConnection,
    token: str,
    part: DownloadedPart,
) -> PartDiff:
    """Reconcile one work-product ``part``'s manifests against the instance.

    Missing = manifest names with no present record. Duplicates = present
    names with more than one record; the oldest (by ``createTime``) is kept
    and the rest are flagged for deletion.
    """
    wpc_kind, name_to_path = index_part_manifests(part)
    present = present_wpc_records(connection, token, wpc_kind)

    expected_names = set(name_to_path)
    present_names = set(present) - {""}
    missing = tuple(sorted(expected_names - present_names))

    extra_ids: list[str] = []
    for name, recs in present.items():
        if name and len(recs) > 1:
            # keep the oldest, delete the rest
            ordered = sorted(recs, key=lambda t: t[1])
            extra_ids.extend(rid for rid, _ in ordered[1:])

    present_records = sum(len(v) for v in present.values())
    return PartDiff(
        part_key=part.key,
        wpc_kind=wpc_kind,
        expected=len(expected_names),
        present_records=present_records,
        missing_names=missing,
        duplicate_extra_ids=tuple(extra_ids),
    )


def sample_bulk(
    connection: ADMEConnection,
    token: str,
    *,
    offsets: Sequence[int] = (0, 500, 3000, 6000, 9000, 9900),
    file_kind: str = FILE_GENERIC_KIND,
) -> list[BlobProbeResult]:
    """Probe blob retrievability for File.Generic records at spread offsets.

    Each offset yields one sampled record whose blob is checked via
    :func:`app.services.files.check_file_blob`. Offsets past the current
    record count are skipped.
    """
    results: list[BlobProbeResult] = []
    for off in offsets:
        page = search_records(
            connection, token, kind=file_kind, limit=1, offset=off
        )
        if not page.ok or not page.records:
            continue
        results.append(check_file_blob(connection, token, page.records[0].id))
    return results


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def repair_missing(
    connection: ADMEConnection,
    token: str,
    part: DownloadedPart,
    diff: PartDiff,
    *,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
    token_provider: Callable[[], str] | None = None,
) -> Iterator[SubmitResult]:
    """Re-submit the manifests for ``diff.missing_names`` via the WP loader."""
    if not diff.missing_names:
        return
    _kind, name_to_path = index_part_manifests(part)
    paths = [name_to_path[n] for n in diff.missing_names if n in name_to_path]
    if not paths:
        return
    yield from submit_work_products(
        paths,
        datasets_root=part.datasets_root,
        acl_owners=acl_owners,
        acl_viewers=acl_viewers,
        legal_tag=legal_tag,
        data_partition_id=connection.data_partition_id,
        connection=connection,
        token=token,
        token_provider=token_provider,
    )


def repair_duplicates(
    connection: ADMEConnection,
    token: str,
    diff: PartDiff,
) -> Iterator[RecordDeleteResult]:
    """Delete each duplicate WPC plus its File datasets and parent WorkProduct.

    Yields one :class:`RecordDeleteResult` per record deleted (WPC, each of
    its ``data.Datasets`` files, and any WorkProduct whose ``data.Components``
    references the WPC).
    """
    for wpc_id in diff.duplicate_extra_ids:
        targets: list[str] = [wpc_id]
        detail = get_record(connection, token, wpc_id)
        if detail.ok and isinstance(detail.record, dict):
            datasets = detail.record.get("data", {}).get("Datasets") or []
            targets.extend(d for d in datasets if isinstance(d, str))
        parents = search_records(
            connection, token, kind=WORK_PRODUCT_KIND,
            query=f'data.Components:("{wpc_id}")', limit=10,
        )
        if parents.ok:
            targets.extend(r.id for r in parents.records)
        for target in targets:
            yield delete_record(connection, token, target)
