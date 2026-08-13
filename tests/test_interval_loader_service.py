"""Tests for the interval-load orchestrator (plan + run_interval)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import SubmitResult
from app.services import interval_loader
from app.services.downloaded_dataset import DownloadedPart
from app.services.load_progress import ResumableProgress


def _connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="opendes",
        auth_method=AuthMethod.USER_IMPERSONATION,
    )


def _part(key: str, section: str | None, is_wp: bool, tmp: Path) -> DownloadedPart:
    return DownloadedPart(
        key=key,
        label=key,
        kind=key.split("/")[0],
        section=section,
        is_work_product=is_wp,
        manifest_dir=tmp,
        manifest_count=0,
        datasets_root=tmp / "datasets",
    )


def _row(name: str, ok: bool = True) -> SubmitResult:
    return SubmitResult(
        manifest_path=Path(name),
        filename=name,
        status="success" if ok else "error",
        run_id=None,
        record_id=name,
        error=None if ok else "boom",
        submitted_at=datetime.now(UTC),
    )


def _all_parts(tmp: Path) -> list[DownloadedPart]:
    return [
        _part("work-products/well logs", None, True, tmp),
        _part("master-data/Wellbore", "MasterData", False, tmp),
        _part("reference-data", "ReferenceData", False, tmp),
        _part("work-products/documents", None, True, tmp),
        _part("master-data/Well", "MasterData", False, tmp),
        _part("master-data/Misc_master_data", "MasterData", False, tmp),
        _part("work-products/well logs_1_1_0", None, True, tmp),
    ]


def test_plan_orders_tiers_and_excludes_v110(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        interval_loader, "discover_parts", lambda root: _all_parts(tmp_path)
    )
    plan = interval_loader.plan_interval(tmp_path)
    keys = [p.key for p in plan]
    assert keys == [
        "reference-data",
        "master-data/Misc_master_data",
        "master-data/Well",
        "master-data/Wellbore",
        "work-products/documents",
        "work-products/well logs",
    ]
    # v110 excluded by default; each plan tags its method.
    assert "work-products/well logs_1_1_0" not in keys
    methods = {p.key: p.method for p in plan}
    assert methods["reference-data"] == "storage"
    assert methods["work-products/documents"] == "dag"


def test_plan_include_v110_and_exclude_wp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        interval_loader, "discover_parts", lambda root: _all_parts(tmp_path)
    )
    keys = [p.key for p in interval_loader.plan_interval(tmp_path, include_v110=True)]
    assert "work-products/well logs_1_1_0" in keys

    no_wp = interval_loader.plan_interval(tmp_path, include_work_products=False)
    assert all(not p.part.is_work_product for p in no_wp)


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, parts: list[DownloadedPart]
) -> dict[str, list[Any]]:
    spy: dict[str, list[Any]] = {"storage": [], "wp": []}
    monkeypatch.setattr(interval_loader, "discover_parts", lambda root: parts)

    def fake_list(part: DownloadedPart, **_: Any) -> list[Path]:
        # two manifests per part, uniquely named
        stem = part.key.replace("/", "_")
        return [tmp_path / f"{stem}_{i}.json" for i in range(2)]

    monkeypatch.setattr(interval_loader, "list_part_manifests", fake_list)

    def fake_storage(paths: Any, **kwargs: Any):
        spy["storage"].append(kwargs)
        for p in paths:
            yield _row(p.name)

    def fake_wp(paths: Any, **kwargs: Any):
        spy["wp"].append((list(paths), kwargs))
        for p in paths:
            yield _row(p.name)

    monkeypatch.setattr(interval_loader, "submit_records_from_paths", fake_storage)
    monkeypatch.setattr(interval_loader, "submit_work_products", fake_wp)
    return spy


def test_run_interval_streams_events_and_uses_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = [
        _part("reference-data", "ReferenceData", False, tmp_path),
        _part("master-data/Well", "MasterData", False, tmp_path),
        _part("work-products/documents", None, True, tmp_path),
    ]
    spy = _patch_run(monkeypatch, tmp_path, parts)

    events = list(
        interval_loader.run_interval(
            tmp_path,
            interval_label="20260908-",
            connection=_connection(),
            acl_owners=["o@x"],
            acl_viewers=["v@x"],
            legal_tag="lt",
            token="tok",
        )
    )
    starts = [e.tier for e in events if e.phase == "tier_start"]
    assert starts == [
        "reference-data",
        "master-data/Well",
        "work-products/documents",
    ]
    # storage tiers ran through submit_records_from_paths with the prefix.
    assert len(spy["storage"]) == 2
    assert all(k["load_prefix"] == "20260908-" for k in spy["storage"])
    assert all(k["overwrite_acl_legal"] is True for k in spy["storage"])
    # work-products ran through submit_work_products with the prefix.
    assert len(spy["wp"]) >= 1
    assert all(k["load_prefix"] == "20260908-" for _p, k in spy["wp"])
    # per-item events carry SubmitResults
    items = [e for e in events if e.phase == "item"]
    assert len(items) == 6  # 3 tiers x 2 manifests
    assert all(e.result is not None for e in items)


def test_run_interval_wp_resumes_from_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = [_part("work-products/markers", None, True, tmp_path)]
    spy = _patch_run(monkeypatch, tmp_path, parts)
    progress = ResumableProgress(tmp_path / "prog.json")
    # Pre-mark the first manifest as already done.
    progress.mark("work-products/markers", "work-products_markers_0.json")

    list(
        interval_loader.run_interval(
            tmp_path,
            interval_label="",
            connection=_connection(),
            acl_owners=["o@x"],
            acl_viewers=["v@x"],
            legal_tag="lt",
            token="tok",
            progress=progress,
        )
    )
    # Only the not-yet-done manifest was submitted.
    submitted = [p.name for paths, _ in spy["wp"] for p in paths]
    assert submitted == ["work-products_markers_1.json"]
    # And it got recorded as complete.
    assert progress.is_done(
        "work-products/markers", "work-products_markers_1.json"
    )


def test_run_interval_abort_stops_before_next_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = [
        _part("reference-data", "ReferenceData", False, tmp_path),
        _part("master-data/Well", "MasterData", False, tmp_path),
    ]
    _patch_run(monkeypatch, tmp_path, parts)

    events = list(
        interval_loader.run_interval(
            tmp_path,
            interval_label="",
            connection=_connection(),
            acl_owners=["o@x"],
            acl_viewers=["v@x"],
            legal_tag="lt",
            token="tok",
            should_abort=lambda: True,  # abort immediately
        )
    )
    # Aborts before starting any tier.
    assert events == []
