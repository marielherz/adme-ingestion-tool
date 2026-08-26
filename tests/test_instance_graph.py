"""Tests for the instance_graph DOT rendering and model."""

from __future__ import annotations

import app.services.instance_graph as instance_graph
from app.services.instance_graph import (
    GraphEdge,
    GraphNode,
    WellGraph,
    build_well_graph,
    to_graphviz_dot,
)


def _sample_graph() -> WellGraph:
    g = WellGraph(label="TNO", well_id="opendes:master-data--Well:2149")
    g.nodes.append(GraphNode("opendes:master-data--Well:2149", "Well"))
    g.nodes.append(GraphNode("opendes:master-data--Wellbore:2149", "Wellbore"))
    g.edges.append(
        GraphEdge(
            "opendes:master-data--Well:2149",
            "opendes:master-data--Wellbore:2149",
            "hasWellbore",
            "explicit-reverse",
        )
    )
    return g


def test_to_dict_roundtrip_counts() -> None:
    g = _sample_graph()
    d = g.to_dict()
    assert d["node_count"] == 2
    assert d["edge_count"] == 1
    assert d["well_id"].endswith("Well:2149")


def test_graphviz_dot_contains_nodes_and_edges() -> None:
    dot = to_graphviz_dot(_sample_graph())
    assert dot.startswith("digraph well {")
    assert "hasWellbore" in dot
    assert "opendes:master-data--Well:2149" in dot
    # Each declared node should appear; the edge uses -> syntax.
    assert "->" in dot
    assert dot.strip().endswith("}")


def test_graphviz_dot_colors_by_role() -> None:
    dot = to_graphviz_dot(_sample_graph())
    # Well and Wellbore have distinct fill colors.
    assert "#1f77b4" in dot  # Well
    assert "#ff7f0e" in dot  # Wellbore


def test_build_well_graph_derives_sidetrack_from_sequence(monkeypatch) -> None:
    well = "opendes:master-data--Well:AUR-01"
    main_bore = "opendes:master-data--Wellbore:AUR-01-01"
    sidetrack = "opendes:master-data--Wellbore:AUR-01-ST1"

    monkeypatch.setattr(
        instance_graph,
        "_wellbores_for_well",
        lambda c, t, w: [(main_bore, 0), (sidetrack, 1)],
    )
    monkeypatch.setattr(instance_graph, "_ids_for", lambda *a, **k: [])

    graph = build_well_graph(None, "token", well)

    sidetrack_edges = [e for e in graph.edges if e.type == "sidetrackOf"]
    assert len(sidetrack_edges) == 1
    edge = sidetrack_edges[0]
    assert edge.source == sidetrack
    assert edge.target == main_bore
    assert edge.provenance == "derived"


def test_build_well_graph_single_bore_has_no_sidetrack(monkeypatch) -> None:
    well = "opendes:master-data--Well:AUR-02"
    monkeypatch.setattr(
        instance_graph,
        "_wellbores_for_well",
        lambda c, t, w: [("opendes:master-data--Wellbore:AUR-02-01", 0)],
    )
    monkeypatch.setattr(instance_graph, "_ids_for", lambda *a, **k: [])

    graph = build_well_graph(None, "token", well)

    assert not [e for e in graph.edges if e.type == "sidetrackOf"]
