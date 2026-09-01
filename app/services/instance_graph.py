"""Materialize a read-only OSDU well-package graph from ADME Search.

Follows the explicit relationship IDs already present in records:
- Wellbore --WellID--> Well (found by reverse lookup from a Well)
- WPC --WellboreID--> Wellbore (one-to-many children per wellbore)

Every edge is provenance-labeled so callers can distinguish explicit links
from any future derived/bridged ones. Shared by the POC script and the
Streamlit Well Graph page, so it takes an already-authenticated connection and
token rather than doing its own auth.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from app.models.connection import ADMEConnection
from app.services.search import search_with_cursor

WELLBORE_KIND = "osdu:wks:master-data--Wellbore:1.0.0"

# WPC roles that hang off a Wellbore via an explicit WellboreID.
CHILD_WPC_KINDS: dict[str, str] = {
    "WellboreTrajectory": "osdu:wks:work-product-component--WellboreTrajectory:1.0.0",
    "WellLog": "osdu:wks:work-product-component--WellLog:1.0.0",
    "WellboreMarkerSet": "osdu:wks:work-product-component--WellboreMarkerSet:1.0.0",
    "WellboreIntervalSet": "osdu:wks:work-product-component--WellboreIntervalSet:1.0.0",
}

# Role -> display color (used by the visualization).
ROLE_COLORS: dict[str, str] = {
    "Concept": "#e6c229",
    "Well": "#1f77b4",
    "Wellbore": "#ff7f0e",
    "WellboreTrajectory": "#2ca02c",
    "WellLog": "#9467bd",
    "WellboreMarkerSet": "#d62728",
    "WellboreIntervalSet": "#8c564b",
}


@dataclass
class GraphNode:
    id: str
    role: str


@dataclass
class GraphEdge:
    source: str
    target: str
    type: str
    provenance: str  # explicit-reverse | derived | unresolved


@dataclass
class WellGraph:
    label: str
    well_id: str
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    api_calls: int = 0
    build_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "well_id": self.well_id,
            "api_calls": self.api_calls,
            "build_seconds": round(self.build_seconds, 3),
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "nodes": [asdict(n) for n in self.nodes],
            "edges": [asdict(e) for e in self.edges],
        }


def _ids_for(
    connection: ADMEConnection,
    token: str,
    kind: str,
    query: str,
    cap: int = 50,
) -> list[str]:
    page = search_with_cursor(
        connection, token, kind=kind, limit=cap, query=query, returned_fields=("id",)
    )
    if not page.ok:
        return []
    results = (page.raw_response or {}).get("results", [])
    return [r.get("id") for r in results if isinstance(r, dict) and r.get("id")]


def _wellbores_for_well(
    connection: ADMEConnection,
    token: str,
    well_id: str,
    cap: int = 50,
) -> list[tuple[str, int | None]]:
    """Return ``(wellbore_id, sequence_number)`` for a well's wellbores.

    OSDU has no standard parent-wellbore field, so sidetrack lineage is
    derived from ``data.SequenceNumber`` (0 = original bore, higher = later
    sidetracks) by :func:`build_well_graph`.
    """
    page = search_with_cursor(
        connection,
        token,
        kind=WELLBORE_KIND,
        limit=cap,
        query=f'data.WellID:"{well_id}:"',
        returned_fields=("id", "data.SequenceNumber"),
    )
    if not page.ok:
        return []
    results = (page.raw_response or {}).get("results", [])
    out: list[tuple[str, int | None]] = []
    for record in results:
        if not isinstance(record, dict):
            continue
        wb_id = record.get("id")
        if not wb_id:
            continue
        sequence: int | None = None
        data = record.get("data")
        if isinstance(data, dict):
            raw_seq = data.get("SequenceNumber")
            if isinstance(raw_seq, int):
                sequence = raw_seq
            elif isinstance(raw_seq, str) and raw_seq.isdigit():
                sequence = int(raw_seq)
        out.append((wb_id, sequence))
    return out


def build_well_graph(
    connection: ADMEConnection,
    token: str,
    well_id: str,
    *,
    label: str = "Well",
) -> WellGraph:
    """Materialize the well package by following explicit edges (read-only)."""
    graph = WellGraph(label=label, well_id=well_id)
    graph.nodes.append(GraphNode(id=well_id, role="Well"))
    start = time.perf_counter()
    calls = 0

    wellbores = _wellbores_for_well(connection, token, well_id)
    calls += 1

    # Derive the base bore (lowest sequence) so higher-sequence bores can be
    # drawn as sidetracks. Only meaningful when a well has >1 wellbore with
    # distinct sequence numbers.
    base_id: str | None = None
    sequenced = [(wb, seq) for wb, seq in wellbores if seq is not None]
    if len(wellbores) > 1 and sequenced:
        base_id = min(sequenced, key=lambda pair: pair[1])[0]

    for wb_id, sequence in wellbores:
        graph.nodes.append(GraphNode(id=wb_id, role="Wellbore"))
        graph.edges.append(GraphEdge(well_id, wb_id, "hasWellbore", "explicit-reverse"))
        # Sidetrack lineage: link a later bore to the base bore.
        if (
            base_id is not None
            and wb_id != base_id
            and sequence is not None
            and sequence > 0
        ):
            graph.edges.append(
                GraphEdge(wb_id, base_id, "sidetrackOf", "derived")
            )
        for role, kind in CHILD_WPC_KINDS.items():
            child_ids = _ids_for(
                connection, token, kind, f'data.WellboreID:"{wb_id}:"'
            )
            calls += 1
            for child_id in child_ids:
                graph.nodes.append(GraphNode(id=child_id, role=role))
                graph.edges.append(
                    GraphEdge(wb_id, child_id, f"has{role}", "explicit-reverse")
                )

    graph.api_calls = calls
    graph.build_seconds = time.perf_counter() - start
    return graph


def _short_label(node_id: str) -> str:
    """Compact label for a node: last id segment, role-agnostic."""
    parts = node_id.split(":")
    if len(parts) >= 3:
        tail = parts[2] or (parts[3] if len(parts) > 3 else "")
        return tail[:14]
    return node_id[:14]


def to_graphviz_dot(graph: WellGraph) -> str:
    """Render the graph as Graphviz DOT for st.graphviz_chart."""
    lines = ["digraph well {", "  rankdir=LR;", '  node [style=filled, fontsize=10];']
    for node in graph.nodes:
        color = ROLE_COLORS.get(node.role, "#cccccc")
        label = f"{node.role}\\n{_short_label(node.id)}"
        lines.append(
            f'  "{node.id}" [label="{label}", fillcolor="{color}", '
            f'fontcolor=white];'
        )
    for edge in graph.edges:
        lines.append(
            f'  "{edge.source}" -> "{edge.target}" [label="{edge.type}", fontsize=8];'
        )
    lines.append("}")
    return "\n".join(lines)
