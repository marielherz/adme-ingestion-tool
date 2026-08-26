#!/usr/bin/env python3
"""Write the Hero Field synthetic dataset to local manifest files.

Generates the coherent Hero 1 (sidetrack / graph) dataset from
:mod:`app.services.hero_field` and writes it under
``app/data/datasets/hero-field/``:

    master-data/   load_Organisation.json, load_Well.json, load_Wellbore.json
    work-products/ load_WellboreMarkerSet.json, load_WellReport.json
    documents/     AUR-01_Final_Well_Report.txt
    hero-field-summary.json   (answer key for demo scripting)

This is a LOCAL, read-only operation — it never contacts ADME. Load the
manifests later via the Bulk Load page or the work-product loader if desired.

Usage:
    python scripts/generate_hero_field.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.services.hero_field import build_hero_field  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "app" / "data" / "datasets" / "hero-field"

# Route each manifest to a sensible subfolder.
_MASTER_DATA = {"load_Organisation.json", "load_Well.json", "load_Wellbore.json"}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=4), encoding="utf-8")


def main() -> None:
    dataset = build_hero_field()

    print("=" * 60)
    print("Hero Field synthetic dataset generation")
    print("=" * 60)

    for name, manifest in dataset.manifests.items():
        subdir = "master-data" if name in _MASTER_DATA else "work-products"
        out_path = OUTPUT_DIR / subdir / name
        _write_json(out_path, manifest)
        rel = out_path.relative_to(PROJECT_ROOT)
        print(f"  wrote {rel}")

    for name, text in dataset.documents.items():
        out_path = OUTPUT_DIR / "documents" / name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"  wrote {out_path.relative_to(PROJECT_ROOT)}")

    summary_path = OUTPUT_DIR / "hero-field-summary.json"
    _write_json(summary_path, dataset.summary)
    print(f"  wrote {summary_path.relative_to(PROJECT_ROOT)}")

    counts = dataset.summary["counts"]
    print("-" * 60)
    print(
        "Field '{field}': {wells} wells, {wellbores} wellbores "
        "(incl. sidetrack), {markersets} marker sets, {reports} report.".format(
            field=dataset.summary["field"], **counts
        )
    )
    print(
        "Hero well {hero} -> sidetrack {st[wellbore]} (parent {st[parent]}).".format(
            hero=dataset.summary["hero_well"], st=dataset.summary["sidetrack"]
        )
    )
    print("Output dir:", OUTPUT_DIR.relative_to(PROJECT_ROOT))


if __name__ == "__main__":
    main()
