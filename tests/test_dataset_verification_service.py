"""Tests for the dataset_verification service (verify + repair)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import (
    BlobProbeResult,
    CursorSearchResult,
    RecordDeleteResult,
    RecordDetailResult,
    RecordSummary,
    SearchPageResult,
    SubmitResult,
)
from app.services import dataset_verification as dv
from app.services.downloaded_dataset import DownloadedPart


def _connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="opendes",
        auth_method=AuthMethod.USER_IMPERSONATION,
    )


def _wp_manifest(name: str) -> dict[str, Any]:
    return {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {
            "WorkProduct": {"data": {"Components": ["surrogate-key:wpc-1"]}},
            "WorkProductComponents": [
                {
                    "id": "surrogate-key:wpc-1",
                    "kind": "osdu:wks:work-product-component--WellboreTrajectory:1.0.0",
                    "data": {"Name": name, "Datasets": ["surrogate-key:file-1"]},
                }
            ],
            "Datasets": [{"id": "surrogate-key:file-1"}],
        },
    }


def _part(tmp_path: Path, names: list[str]) -> DownloadedPart:
    mdir = tmp_path / "trajectories"
    mdir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (mdir / f"load_{n}.json").write_text(
            json.dumps(_wp_manifest(n)), encoding="utf-8"
        )
    return DownloadedPart(
        key="work-products/trajectories",
        label="trajectories",
        kind="work-products",
        section=None,
        is_work_product=True,
        manifest_dir=mdir,
        manifest_count=len(names),
        datasets_root=tmp_path / "datasets",
    )


def _summary(rid: str, name: str, ctime: str = "") -> RecordSummary:
    return RecordSummary(
        id=rid,
        kind="osdu:wks:work-product-component--WellboreTrajectory:1.0.0",
        create_time=ctime,
        source={"data": {"Name": name}},
    )


def _cursor_page(records: list[RecordSummary]) -> CursorSearchResult:
    return CursorSearchResult(
        kind="k", cursor=None, records=records, has_more=False,
        ok=True, http_status=200,
    )


# ---------------------------------------------------------------------------
# Manifest side (pure)
# ---------------------------------------------------------------------------


def test_read_wpc_kind_and_name(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    p.write_text(json.dumps(_wp_manifest("9157.csv")), encoding="utf-8")
    kind, name = dv.read_wpc_kind_and_name(p)
    assert kind == "osdu:wks:work-product-component--WellboreTrajectory:1.0.0"
    assert name == "9157.csv"


def test_read_wpc_kind_and_name_bad_file(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert dv.read_wpc_kind_and_name(p) == ("", "")


def test_index_part_manifests(tmp_path: Path) -> None:
    part = _part(tmp_path, ["a.csv", "b.csv"])
    kind, mapping = dv.index_part_manifests(part)
    assert kind.endswith("WellboreTrajectory:1.0.0")
    assert set(mapping) == {"a.csv", "b.csv"}
    assert all(isinstance(v, Path) for v in mapping.values())


# ---------------------------------------------------------------------------
# Instance side
# ---------------------------------------------------------------------------


def test_count_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dv, "search_records",
        lambda *a, **k: SearchPageResult(kind="k", ok=True, total_count=929),
    )
    assert dv.count_kind(_connection(), "t", "osdu:wks:...:*") == 929


def test_count_kinds_flags_ok_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = {"a": 9, "b": 10_000}
    monkeypatch.setattr(
        dv, "search_records",
        lambda *a, **k: SearchPageResult(
            kind=k["kind"], ok=True, total_count=counts[k["kind"]]
        ),
    )
    rows = dv.count_kinds(
        _connection(), "t", [("A", "a", 9), ("B", "b", None)]
    )
    assert rows[0].ok and rows[0].delta == 0
    assert rows[1].expected is None and rows[1].capped is True


def test_present_wpc_records_groups_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _cursor_page([
        _summary("id-1", "a.csv", "2026-01-01"),
        _summary("id-2", "b.csv", "2026-01-02"),
        _summary("id-3", "b.csv", "2026-01-03"),  # dup of b
    ])
    monkeypatch.setattr(dv, "export_all_records", lambda *a, **k: iter([page]))
    grouped = dv.present_wpc_records(_connection(), "t", "osdu:wks:x:1.0.0")
    assert set(grouped) == {"a.csv", "b.csv"}
    assert len(grouped["b.csv"]) == 2


def test_diff_part_finds_missing_and_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # manifests a, b, c ; present: a (once), b (twice=dup), c missing
    part = _part(tmp_path, ["a.csv", "b.csv", "c.csv"])
    page = _cursor_page([
        _summary("id-a", "a.csv", "2026-01-01"),
        _summary("id-b-old", "b.csv", "2026-01-01"),
        _summary("id-b-new", "b.csv", "2026-01-05"),
    ])
    monkeypatch.setattr(dv, "export_all_records", lambda *a, **k: iter([page]))

    diff = dv.diff_part(_connection(), "t", part)
    assert diff.missing_names == ("c.csv",)
    # oldest b kept, newest flagged for deletion
    assert diff.duplicate_extra_ids == ("id-b-new",)
    assert diff.expected == 3
    assert diff.present_records == 3
    assert diff.unique_present == 2
    assert diff.clean is False


def test_diff_part_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    part = _part(tmp_path, ["a.csv", "b.csv"])
    page = _cursor_page([_summary("id-a", "a.csv"), _summary("id-b", "b.csv")])
    monkeypatch.setattr(dv, "export_all_records", lambda *a, **k: iter([page]))
    diff = dv.diff_part(_connection(), "t", part)
    assert diff.clean is True
    assert diff.missing_names == ()
    assert diff.duplicate_extra_ids == ()


def test_sample_bulk_probes_each_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_search(conn, token, *, kind, limit, offset=0, **k):
        # only offsets 0 and 500 have a record
        if offset in (0, 500):
            return SearchPageResult(
                kind=kind, ok=True,
                records=[RecordSummary(id=f"file-{offset}", kind=kind)],
            )
        return SearchPageResult(kind=kind, ok=True, records=[])

    probed: list[str] = []

    def fake_probe(conn, token, rid, **k):
        probed.append(rid)
        return BlobProbeResult(record_id=rid, present=True, blob_http_status=206)

    monkeypatch.setattr(dv, "search_records", fake_search)
    monkeypatch.setattr(dv, "check_file_blob", fake_probe)

    out = dv.sample_bulk(_connection(), "t", offsets=(0, 500, 9999))
    assert probed == ["file-0", "file-500"]
    assert all(r.present for r in out)


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def test_repair_missing_resubmits_missing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    part = _part(tmp_path, ["a.csv", "b.csv", "c.csv"])
    diff = dv.PartDiff(
        part_key=part.key, wpc_kind="k", expected=3, present_records=2,
        missing_names=("c.csv",),
    )
    seen: dict[str, Any] = {}

    def fake_submit(paths, **kwargs):
        seen["paths"] = list(paths)
        seen["kwargs"] = kwargs
        yield SubmitResult(
            manifest_path=paths[0], filename=paths[0].name, status="success",
            run_id="r1", record_id=None, error=None,
            submitted_at=datetime.now(UTC),
        )

    monkeypatch.setattr(dv, "submit_work_products", fake_submit)
    results = list(
        dv.repair_missing(
            _connection(), "t", part, diff,
            acl_owners=["o"], acl_viewers=["v"], legal_tag="lt",
        )
    )
    assert len(results) == 1 and results[0].status == "success"
    assert [p.name for p in seen["paths"]] == ["load_c.csv.json"]
    assert seen["kwargs"]["legal_tag"] == "lt"


def test_repair_missing_noop_when_nothing_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    part = _part(tmp_path, ["a.csv"])
    diff = dv.PartDiff(part_key=part.key, wpc_kind="k", expected=1, present_records=1)
    called = {"n": 0}
    monkeypatch.setattr(
        dv, "submit_work_products",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or iter([]),
    )
    assert list(dv.repair_missing(
        _connection(), "t", part, diff,
        acl_owners=["o"], acl_viewers=["v"], legal_tag="lt",
    )) == []
    assert called["n"] == 0


def test_repair_duplicates_deletes_wpc_files_and_parent(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    diff = dv.PartDiff(
        part_key="work-products/trajectories", wpc_kind="k", expected=1,
        present_records=2, duplicate_extra_ids=("wpc-dup",),
    )

    def fake_get_record(conn, token, rid):
        return RecordDetailResult(
            record_id=rid, ok=True,
            record={"data": {"Datasets": ["file-1:", "file-2:"]}},
        )

    def fake_search(conn, token, *, kind, query=None, limit=10, **k):
        return SearchPageResult(
            kind=kind, ok=True,
            records=[RecordSummary(id="wp-parent", kind=kind)],
        )

    deleted: list[str] = []

    def fake_delete(conn, token, rid):
        deleted.append(rid)
        return RecordDeleteResult(record_id=rid, ok=True, http_status=204)

    monkeypatch.setattr(dv, "get_record", fake_get_record)
    monkeypatch.setattr(dv, "search_records", fake_search)
    monkeypatch.setattr(dv, "delete_record", fake_delete)

    results = list(dv.repair_duplicates(_connection(), "t", diff))
    assert all(r.ok for r in results)
    # WPC + 2 files + 1 parent WP
    assert deleted == ["wpc-dup", "file-1:", "file-2:", "wp-parent"]
