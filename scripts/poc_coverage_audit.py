#!/usr/bin/env python
"""Read-only coverage audit: relationship-population rates + Document linkage.

Answers two questions the ADME Graph spec depends on:
1. Across the whole instance, what fraction of each priority kind actually
   populates each structural relationship edge? (Feeds the "how instantiable
   is the topology" / coverage-boundaries requirement.)
2. Can Documents be reached at all? Documents have no Wellbore/Well edge, so
   we probe whether WorkProduct grouping links them back to the package.

All reads only (Search v2 counts + a few sample records). Writes
``poc-output/coverage-audit.csv``. Run ``az login`` first.
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.services.search import search_with_cursor  # noqa: E402
from scripts.sample_wpc_text import _connection, _token  # noqa: E402

OUT_DIR = _REPO_ROOT / "poc-output"

# (kind, [structural relationship fields to measure])
AUDIT_TARGETS = [
    ("master-data--Well:1.0.0", []),
    ("master-data--Wellbore:1.0.0", ["WellID"]),
    ("work-product-component--WellboreTrajectory:1.0.0", ["WellboreID"]),
    ("work-product-component--WellLog:1.0.0", ["WellboreID"]),
    ("work-product-component--WellboreMarkerSet:1.0.0", ["WellboreID"]),
    ("work-product-component--WellboreIntervalSet:1.0.0", ["WellboreID"]),
    ("work-product-component--Document:1.0.0", ["DocumentTypeID"]),
]


def _count(conn, tok, kind: str, query: str | None) -> int | None:
    page = search_with_cursor(
        conn, tok, kind=kind, limit=1, query=query, returned_fields=("id",)
    )
    if not page.ok:
        return None
    return (page.raw_response or {}).get("totalCount")


def _sample(conn, tok, kind: str, fields: tuple[str, ...], n: int = 3) -> list[dict]:
    page = search_with_cursor(
        conn, tok, kind=kind, limit=n, returned_fields=fields
    )
    if not page.ok:
        return []
    return (page.raw_response or {}).get("results", [])


def audit_population(conn, tok) -> list[dict]:
    rows: list[dict] = []
    for suffix, fields in AUDIT_TARGETS:
        kind = f"osdu:wks:{suffix}"
        total = _count(conn, tok, kind, "*")
        if not fields:
            rows.append(
                {
                    "kind": suffix,
                    "field": "(none)",
                    "total": total,
                    "populated": total,
                    "pct": 100.0 if total else 0.0,
                }
            )
            continue
        for field_name in fields:
            populated = _count(conn, tok, kind, f"data.{field_name}:*")
            pct = (
                round(100.0 * populated / total, 1)
                if total and populated is not None
                else 0.0
            )
            rows.append(
                {
                    "kind": suffix,
                    "field": field_name,
                    "total": total,
                    "populated": populated,
                    "pct": pct,
                }
            )
    return rows


def probe_document_linkage(conn, tok) -> dict:
    """Check whether WorkProduct records group Components (incl. Documents)."""
    wp_kind = "osdu:wks:work-product--WorkProduct:1.0.0"
    wp_total = _count(conn, tok, wp_kind, "*")
    doc_total = _count(conn, tok, "osdu:wks:work-product-component--Document:1.0.0", "*")

    # Sample WorkProduct records and look for a Components / *ComponentIDs field.
    samples = _sample(conn, tok, wp_kind, ("id", "data"), n=5)
    component_field = None
    references_document = False
    example_component_ids: list[str] = []
    for r in samples:
        data = r.get("data") if isinstance(r, dict) else None
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if "Component" in key and isinstance(value, list) and value:
                component_field = key
                for item in value[:5]:
                    if isinstance(item, str):
                        example_component_ids.append(item)
                        if "Document" in item:
                            references_document = True
        if component_field:
            break

    return {
        "workproduct_total": wp_total,
        "document_total": doc_total,
        "component_field": component_field or "(none found)",
        "workproduct_references_document": references_document,
        "example_component_ids": example_component_ids[:5],
    }


def write_csv(rows: list[dict]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "coverage-audit.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["Kind", "Relationship field", "Total records", "Populated", "Populated %"]
        )
        for row in rows:
            writer.writerow(
                [row["kind"], row["field"], row["total"], row["populated"], row["pct"]]
            )
    return path


def main() -> int:
    os.environ.setdefault("ADME_TOKEN_SCOPE", "https://energy.azure.com/.default")
    conn = _connection()
    tok = _token(conn)

    print("Coverage audit (read-only)")
    print("=" * 60)
    rows = audit_population(conn, tok)
    print(f"{'Kind':<48}{'Field':<16}{'Total':>8}{'Pop':>8}{'%':>7}")
    for row in rows:
        print(
            f"{row['kind']:<48}{row['field']:<16}"
            f"{str(row['total']):>8}{str(row['populated']):>8}{str(row['pct']):>7}"
        )

    print("\nDocument linkage probe")
    print("-" * 60)
    link = probe_document_linkage(conn, tok)
    for key, value in link.items():
        print(f"  {key}: {value}")

    path = write_csv(rows)
    print(f"\nWrote {path}")

    # Append the document-linkage summary to a small text artifact.
    summary = OUT_DIR / "document-linkage.md"
    lines = ["# Document linkage probe\n"]
    for key, value in link.items():
        lines.append(f"- **{key}**: {value}")
    summary.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
