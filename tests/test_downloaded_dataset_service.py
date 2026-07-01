"""Tests for downloaded-dataset discovery (TNO/Volve external root)."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.downloaded_dataset import (
    datasets_root_for,
    discover_parts,
    list_part_manifests,
)


def _build_download(tmp_path: Path) -> Path:
    """Create a minimal osdu-data-load-tno layout under tmp_path/tno."""
    root = tmp_path / "tno"
    provided = root / "TNO" / "provided"
    (root / "datasets" / "well-logs").mkdir(parents=True)
    (root / "datasets" / "documents").mkdir(parents=True)

    ref = provided / "reference-data"
    ref.mkdir(parents=True)
    for name in ("load_refA.OPEN.json", "load_refB.OPEN.json"):
        (ref / name).write_text(
            json.dumps({"kind": "k", "ReferenceData": []}), encoding="utf-8"
        )

    for sub, n in (("Well", 2), ("Wellbore", 3)):
        d = provided / "master-data" / sub
        d.mkdir(parents=True)
        for i in range(n):
            (d / f"load_{sub}_{i}.json").write_text(
                json.dumps({"kind": "k", "MasterData": []}), encoding="utf-8"
            )

    for sub, n in (("documents", 1), ("well logs", 2)):
        d = provided / "work-products" / sub
        d.mkdir(parents=True)
        for i in range(n):
            (d / f"load_{i}.json").write_text(
                json.dumps({"kind": "k", "Data": {}}), encoding="utf-8"
            )
    return root


def test_datasets_root_for_finds_blob_root(tmp_path: Path) -> None:
    root = _build_download(tmp_path)
    assert datasets_root_for(root) == root / "datasets"


def test_discover_parts_orders_and_counts(tmp_path: Path) -> None:
    root = _build_download(tmp_path)
    parts = discover_parts(root)
    keys = [p.key for p in parts]

    assert keys == [
        "reference-data",
        "master-data/Well",
        "master-data/Wellbore",
        "work-products/documents",
        "work-products/well logs",
    ]
    by_key = {p.key: p for p in parts}
    assert by_key["reference-data"].section == "ReferenceData"
    assert by_key["reference-data"].manifest_count == 2
    assert by_key["master-data/Wellbore"].section == "MasterData"
    assert by_key["master-data/Wellbore"].manifest_count == 3
    wp = by_key["work-products/well logs"]
    assert wp.is_work_product is True
    assert wp.section is None
    assert wp.datasets_root == root / "datasets"


def test_discover_parts_unknown_root_is_empty(tmp_path: Path) -> None:
    assert discover_parts(tmp_path / "nope") == []


def test_list_part_manifests_limit(tmp_path: Path) -> None:
    root = _build_download(tmp_path)
    well = next(
        p for p in discover_parts(root) if p.key == "master-data/Wellbore"
    )
    assert len(list_part_manifests(well)) == 3
    assert len(list_part_manifests(well, limit=2)) == 2
    assert len(list_part_manifests(well, limit=0)) == 3
