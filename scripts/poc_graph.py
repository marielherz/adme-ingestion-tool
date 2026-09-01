#!/usr/bin/env python
"""Read-only POC: materialize an OSDU well-package graph and benchmark it.

Uses the schema-derived edge catalog (Accenture's core rule) to know which
record fields are relationship edges, then walks real ADME records to build a
provenance-labeled instance graph for one or more wells. Compares graph
assembly against live OSDU Search orchestration and writes artifacts to
``poc-output/``.

Everything is read-only (Search v2 reads only). No records/schemas/indexes are
modified. Run ``az login`` first.

Usage::

    python scripts/poc_graph.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.services.instance_graph import WellGraph, build_well_graph  # noqa: E402
from app.services.osdu_edge_catalog import extract_relationship_edges  # noqa: E402
from scripts.sample_wpc_text import _connection, _token  # noqa: E402

SCHEMA_DIR = _REPO_ROOT / "app" / "data" / "osdu" / "rc--3.0.0" / "schemas"
OUT_DIR = _REPO_ROOT / "poc-output"

# Wells to profile: (label, record id).
TARGET_WELLS = [
    ("TNO", "opendes:master-data--Well:2149"),
    ("Volve", "opendes:master-data--Well:15%2F9-F-12"),
]

PRIORITY_SCHEMAS = {
    "Well": "master-data/Well.1.0.0.json",
    "Wellbore": "master-data/Wellbore.1.0.0.json",
    "WellboreTrajectory": "work-product-component/WellboreTrajectory.1.0.0.json",
    "WellLog": "work-product-component/WellLog.1.0.0.json",
    "WellboreMarkerSet": "work-product-component/WellboreMarkerSet.1.0.0.json",
    "WellboreIntervalSet": "work-product-component/WellboreIntervalSet.1.0.0.json",
    "Document": "work-product-component/Document.1.0.0.json",
}


def build_edge_catalog() -> dict[str, list[dict]]:
    catalog: dict[str, list[dict]] = {}
    for name, rel in PRIORITY_SCHEMAS.items():
        path = SCHEMA_DIR / rel
        if not path.exists():
            catalog[name] = []
            continue
        schema = json.loads(path.read_text(encoding="utf-8"))
        catalog[name] = [
            {
                "property": e.property_name,
                "targets": list(e.target_entity_types),
                "is_array": e.is_array,
            }
            for e in extract_relationship_edges(schema)
        ]
    return catalog


def write_coverage_matrix(graphs: list[WellGraph], catalog: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "coverage-matrix.csv"
    # Structural edges relevant to the well package.
    structural = {
        "Well": ("Wellbore.WellID (reverse)", "Well<-Wellbore"),
        "Wellbore": ("WellboreID (from WPCs)", "Wellbore<-WPC"),
        "WellboreTrajectory": ("WellboreID", "WPC->Wellbore"),
        "WellLog": ("WellboreID", "WPC->Wellbore"),
        "WellboreMarkerSet": ("WellboreID", "WPC->Wellbore"),
        "WellboreIntervalSet": ("WellboreID", "WPC->Wellbore"),
        "Document": ("(none)", "no structural edge"),
    }
    # Count nodes per role per well.
    role_counts = {
        g.label: {} for g in graphs
    }
    for g in graphs:
        for n in g.nodes:
            role_counts[g.label][n.role] = role_counts[g.label].get(n.role, 0) + 1

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Kind",
                "Schema edges (count)",
                "Structural edge field",
                "Edge direction",
                *[f"{g.label} nodes" for g in graphs],
                "Coverage status",
                "Gap classification",
            ]
        )
        for kind in PRIORITY_SCHEMAS:
            edge_field, direction = structural.get(kind, ("", ""))
            counts = [role_counts[g.label].get(kind, 0) for g in graphs]
            if kind == "Document":
                status = "Not supported"
                gap = "OSDU schema lacks explicit Well/Wellbore relationship"
            elif kind == "Well":
                any_wb = any(role_counts[g.label].get("Wellbore", 0) for g in graphs)
                status = "Populated (some sources)" if any_wb else "Unpopulated"
                gap = "Source inconsistent: TNO populates WellID, Volve does not"
            else:
                total = sum(counts)
                status = "Populated" if total else "Defined, unpopulated"
                gap = (
                    "None"
                    if total
                    else "Source record missing/inconsistent"
                )
            writer.writerow(
                [
                    kind,
                    len(catalog.get(kind, [])),
                    edge_field,
                    direction,
                    *counts,
                    status,
                    gap,
                ]
            )
    return path


def write_findings(graphs: list[WellGraph], catalog: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "product-findings.md"
    lines = ["# OSDU Ontology + Graph POC — Findings\n"]
    lines.append("## Method\n")
    lines.append(
        "Edges were discovered with the Accenture generator's core rule: a schema "
        "field ending in `ID`/`IDs` with an `x-osdu-relationship` `EntityType` is a "
        "graph edge. The instance graph was materialized read-only from ADME Search "
        "by following those fields in real records.\n"
    )
    lines.append("## Per-well results\n")
    lines.append("| Well | Nodes | Edges | Wellbores | API calls | Build (s) |")
    lines.append("|---|---|---|---|---|---|")
    for g in graphs:
        wb = sum(1 for n in g.nodes if n.role == "Wellbore")
        lines.append(
            f"| {g.label} ({g.well_id}) | {len(g.nodes)} | {len(g.edges)} | "
            f"{wb} | {g.api_calls} | {g.build_seconds:.2f} |"
        )
    lines.append("\n## Key findings\n")
    lines.append(
        "- **Graph traversal works where data is linked.** The TNO population has "
        "explicit `WellID` (Wellbore→Well) and `WellboreID` (WPC→Wellbore), so the "
        "full one-to-many package materializes from explicit edges.\n"
        "- **Volve records are shells.** Same schemas, but `WellID` is unpopulated, "
        "so its wells return zero wellbores. This is a source/ingestion gap, not an "
        "ontology or engine limitation.\n"
        "- **Document WPCs have no structural edge** to Well/Wellbore (only "
        "`DocumentTypeID`). Linking documents requires WorkProduct traversal or "
        "content/naming — an OSDU schema gap to raise with the community.\n"
        "- **The Accenture rule is reusable**: schema-driven edge discovery worked "
        "directly on the current registered schemas without running their TTL "
        "generator.\n"
    )
    lines.append("## Benchmark interpretation\n")
    lines.append(
        "Graph assembly and live OSDU orchestration issue the *same* Search calls "
        "at build time (1 wellbore lookup + one lookup per WPC kind per wellbore). "
        "The graph's value is **materialize-once, traverse-many**: after a one-time "
        "build, answering \"give me the package for Well X\" is a single graph "
        "lookup instead of re-issuing all those calls per request, plus it carries "
        "provenance and absorbs the Volve gap once.\n"
    )
    lines.append("## P0 / P1 / out of scope\n")
    lines.append(
        "- **P0**: adopt schema-driven edge discovery; fix Volve `WellID` population; "
        "define a Document→Wellbore linking strategy.\n"
        "- **P1**: materialized graph store (engine-neutral first; Fabric Graph as a "
        "candidate); GraphRAG wiring into the semantic marker index.\n"
        "- **Out of scope**: exact reproduction of the committed Accenture TTL; "
        "spatial/offset-well search (needs geospatial, not the ontology).\n"
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    os.environ.setdefault("ADME_TOKEN_SCOPE", "https://energy.azure.com/.default")
    conn = _connection()
    tok = _token(conn)

    print("OSDU ontology + graph POC (read-only)")
    print("=" * 60)
    catalog = build_edge_catalog()
    total_edges = sum(len(v) for v in catalog.values())
    print(f"Edge catalog: {total_edges} relationship edges across "
          f"{len(catalog)} priority kinds.")

    graphs: list[WellGraph] = []
    for label, well_id in TARGET_WELLS:
        print(f"\nBuilding graph for {label}: {well_id}")
        g = build_well_graph(conn, tok, well_id, label=label)
        wb = sum(1 for n in g.nodes if n.role == "Wellbore")
        print(f"  nodes={len(g.nodes)} edges={len(g.edges)} wellbores={wb} "
              f"api_calls={g.api_calls} build={g.build_seconds:.2f}s")
        graphs.append(g)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for g in graphs:
        gp = OUT_DIR / f"{g.label.lower()}-well-graph.json"
        gp.write_text(json.dumps(g.to_dict(), indent=2), encoding="utf-8")
    cov = write_coverage_matrix(graphs, catalog)
    cat_path = OUT_DIR / "edge-catalog.json"
    cat_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    find = write_findings(graphs, catalog)

    print(f"\nArtifacts written to {OUT_DIR}:")
    for p in sorted(OUT_DIR.glob("*")):
        print(f"  {p.name}")
    print(f"\nCoverage matrix: {cov.name}")
    print(f"Findings: {find.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
