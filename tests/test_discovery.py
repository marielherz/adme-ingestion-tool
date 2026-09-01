"""Tests for the combined semantic -> graph discovery service."""

from __future__ import annotations

import app.services.discovery as discovery
from app.services.discovery import (
    DiscoveryResult,
    discover_catalog,
    merge_discovery_graph,
    resolve_anchor_wells,
)
from app.services.instance_graph import GraphEdge, GraphNode, WellGraph
from app.services.marker_search import MarkerHit
from app.services.semantic_catalog import CatalogHit


def _hit(name: str, wellbores: list[str]) -> MarkerHit:
    return MarkerHit(
        marker_name=name,
        geological_ages=["Permian"],
        occurrence_count=10,
        wellbore_count=len(wellbores),
        depth_min=1000.0,
        depth_max=2000.0,
        score=0.9,
        reranker_score=2.1,
        example_wellbores=wellbores,
    )


def test_resolve_anchor_wells_maps_concepts_and_dedupes(monkeypatch) -> None:
    # Two concepts share a wellbore; one wellbore is unresolvable.
    wb_to_well = {
        "opendes:master-data--Wellbore:A": "opendes:master-data--Well:1",
        "opendes:master-data--Wellbore:B": "opendes:master-data--Well:2",
        "opendes:master-data--Wellbore:C": None,
    }
    calls: list[str] = []

    def fake_resolve(connection, token, wellbore_id):
        calls.append(wellbore_id)
        return wb_to_well.get(wellbore_id)

    monkeypatch.setattr(discovery, "_wellbore_to_well", fake_resolve)

    concepts = [
        _hit("Salt", ["opendes:master-data--Wellbore:A"]),
        _hit(
            "Sand",
            [
                "opendes:master-data--Wellbore:A",  # shared -> cached, no extra call
                "opendes:master-data--Wellbore:B",
                "opendes:master-data--Wellbore:C",  # unresolvable
            ],
        ),
    ]

    well_ids, concept_wells, resolve_calls = resolve_anchor_wells(
        None, "token", concepts, max_wells=5
    )

    assert well_ids == [
        "opendes:master-data--Well:1",
        "opendes:master-data--Well:2",
    ]
    assert concept_wells["Salt"] == ["opendes:master-data--Well:1"]
    assert concept_wells["Sand"] == [
        "opendes:master-data--Well:1",
        "opendes:master-data--Well:2",
    ]
    # A, B, C resolved once each (A cached on second use).
    assert resolve_calls == 3
    assert calls.count("opendes:master-data--Wellbore:A") == 1


def test_resolve_anchor_wells_respects_max_wells(monkeypatch) -> None:
    monkeypatch.setattr(
        discovery,
        "_wellbore_to_well",
        lambda c, t, wb: f"opendes:master-data--Well:{wb[-1]}",
    )
    concepts = [
        _hit(
            "Multi",
            [
                "opendes:master-data--Wellbore:1",
                "opendes:master-data--Wellbore:2",
                "opendes:master-data--Wellbore:3",
            ],
        )
    ]
    well_ids, _concept_wells, _calls = resolve_anchor_wells(
        None, "token", concepts, max_wells=2
    )
    assert well_ids == [
        "opendes:master-data--Well:1",
        "opendes:master-data--Well:2",
    ]


def test_merge_discovery_graph_adds_concept_bridge() -> None:
    graph = WellGraph(label="Well", well_id="opendes:master-data--Well:1")
    graph.nodes.append(GraphNode("opendes:master-data--Well:1", "Well"))
    graph.nodes.append(GraphNode("opendes:master-data--Wellbore:A", "Wellbore"))
    graph.edges.append(
        GraphEdge(
            "opendes:master-data--Well:1",
            "opendes:master-data--Wellbore:A",
            "hasWellbore",
            "explicit-reverse",
        )
    )
    result = DiscoveryResult(
        query="salt seals",
        concepts=[_hit("Salt", ["opendes:master-data--Wellbore:A"])],
        anchor_well_ids=["opendes:master-data--Well:1"],
        graphs=[graph],
        concept_wells={"Salt": ["opendes:master-data--Well:1"]},
    )

    merged = merge_discovery_graph(result)

    node_ids = {n.id for n in merged.nodes}
    assert "concept::Salt" in node_ids
    assert "opendes:master-data--Well:1" in node_ids
    concept_node = next(n for n in merged.nodes if n.id == "concept::Salt")
    assert concept_node.role == "Concept"
    bridge = next(e for e in merged.edges if e.type == "matches")
    assert bridge.source == "concept::Salt"
    assert bridge.target == "opendes:master-data--Well:1"
    assert bridge.provenance == "semantic"


def test_merge_discovery_graph_dedupes_shared_nodes() -> None:
    shared = "opendes:master-data--Wellbore:A"
    g1 = WellGraph(label="Well", well_id="opendes:master-data--Well:1")
    g1.nodes.append(GraphNode(shared, "Wellbore"))
    g2 = WellGraph(label="Well", well_id="opendes:master-data--Well:2")
    g2.nodes.append(GraphNode(shared, "Wellbore"))
    result = DiscoveryResult(query="q", graphs=[g1, g2])

    merged = merge_discovery_graph(result)

    assert sum(1 for n in merged.nodes if n.id == shared) == 1


def _catalog_hit(
    title: str,
    *,
    well: str | None = None,
    wellbore: str | None = None,
    source: str = "document",
) -> CatalogHit:
    return CatalogHit(
        record_id=f"opendes:x:{title}",
        source=source,
        kind="osdu:wks:work-product-component--Document:1.0.0",
        title=title,
        content="hazard text",
        anchor_well_id=well,
        anchor_wellbore_id=wellbore,
        score=0.8,
        reranker_score=1.9,
    )


def test_discover_catalog_resolves_well_and_wellbore_anchors(monkeypatch) -> None:
    hits = [
        _catalog_hit("Well hit", well="opendes:master-data--Well:1", source="well"),
        _catalog_hit(
            "Report hit", wellbore="opendes:master-data--Wellbore:B"
        ),
    ]
    monkeypatch.setattr(
        discovery, "search_catalog", lambda *a, **k: hits
    )
    monkeypatch.setattr(
        discovery,
        "_wellbore_to_well",
        lambda c, t, wb: "opendes:master-data--Well:2",
    )
    monkeypatch.setattr(
        discovery,
        "build_well_graph",
        lambda c, t, well_id, label="Well": WellGraph(
            label=label, well_id=well_id
        ),
    )

    result = discover_catalog(None, None, "token", "shallow gas", max_wells=3)

    assert result.catalog_hits == hits
    assert result.anchor_well_ids == [
        "opendes:master-data--Well:1",
        "opendes:master-data--Well:2",
    ]
    assert result.concept_wells["Well hit"] == ["opendes:master-data--Well:1"]
    assert result.concept_wells["Report hit"] == ["opendes:master-data--Well:2"]
    assert len(result.graphs) == 2


def test_discover_unified_merges_marker_and_catalog_anchors(monkeypatch) -> None:
    concepts = [_hit("Hugin", ["opendes:master-data--Wellbore:A"])]
    catalog_hits = [
        _catalog_hit(
            "AUR-01 Final Well Report",
            wellbore="opendes:master-data--Wellbore:B",
        )
    ]
    monkeypatch.setattr(discovery, "search_markers", lambda *a, **k: concepts)
    monkeypatch.setattr(discovery, "search_catalog", lambda *a, **k: catalog_hits)

    wb_to_well = {
        "opendes:master-data--Wellbore:A": "opendes:master-data--Well:1",
        "opendes:master-data--Wellbore:B": "opendes:master-data--Well:2",
    }
    monkeypatch.setattr(
        discovery,
        "_wellbore_to_well",
        lambda c, t, wb: wb_to_well.get(wb),
    )
    monkeypatch.setattr(
        discovery,
        "build_well_graph",
        lambda c, t, well_id, label="Well": WellGraph(
            label=label, well_id=well_id
        ),
    )

    result = discovery.discover_unified(
        None, None, None, "token", "shallow gas near Hugin", max_wells=5
    )

    assert result.concepts == concepts
    assert result.catalog_hits == catalog_hits
    assert result.anchor_well_ids == [
        "opendes:master-data--Well:1",
        "opendes:master-data--Well:2",
    ]
    assert result.concept_wells["Hugin"] == ["opendes:master-data--Well:1"]
    assert result.concept_wells["AUR-01 Final Well Report"] == [
        "opendes:master-data--Well:2"
    ]
    assert len(result.graphs) == 2


def test_discover_unified_survives_one_source_failing(monkeypatch) -> None:
    from app.services.marker_search import MarkerSearchError

    catalog_hits = [
        _catalog_hit("Well hit", well="opendes:master-data--Well:9", source="well")
    ]

    def _boom(*a, **k):
        raise MarkerSearchError("markers index missing")

    monkeypatch.setattr(discovery, "search_markers", _boom)
    monkeypatch.setattr(discovery, "search_catalog", lambda *a, **k: catalog_hits)
    monkeypatch.setattr(
        discovery,
        "build_well_graph",
        lambda c, t, well_id, label="Well": WellGraph(
            label=label, well_id=well_id
        ),
    )

    result = discovery.discover_unified(None, None, None, "token", "well hit")

    assert result.concepts == []
    assert result.catalog_hits == catalog_hits
    assert result.anchor_well_ids == ["opendes:master-data--Well:9"]


def test_interpret_unified_defers_graph_expansion(monkeypatch) -> None:
    concepts = [_hit("Hugin", ["opendes:master-data--Wellbore:A"])]
    monkeypatch.setattr(discovery, "search_markers", lambda *a, **k: concepts)
    monkeypatch.setattr(discovery, "search_catalog", lambda *a, **k: [])
    monkeypatch.setattr(
        discovery,
        "_wellbore_to_well",
        lambda c, t, wb: "opendes:master-data--Well:1",
    )
    # build_well_graph must NOT be called during interpretation.
    monkeypatch.setattr(
        discovery,
        "build_well_graph",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not expand")),
    )

    result = discovery.interpret_unified(
        None, None, None, "token", "Hugin reservoir"
    )

    assert result.anchor_well_ids == ["opendes:master-data--Well:1"]
    assert result.graphs == []


def test_expand_wells_builds_one_graph_per_well(monkeypatch) -> None:
    monkeypatch.setattr(
        discovery,
        "build_well_graph",
        lambda c, t, well_id, label="Well": WellGraph(
            label=label, well_id=well_id
        ),
    )
    graphs, calls = discovery.expand_wells(
        None,
        "token",
        ["opendes:master-data--Well:1", "opendes:master-data--Well:2"],
    )
    assert [g.well_id for g in graphs] == [
        "opendes:master-data--Well:1",
        "opendes:master-data--Well:2",
    ]
    assert calls == 0
