"""Combined discovery: semantic concept match -> graph expansion (GraphRAG).

Implements the "semantic interprets, graph assembles" flow:
1. Interpret the natural-language query by matching semantic concepts
   (stratigraphic marker/formation terms) in the Azure AI Search index.
2. Resolve anchor wells from each concept's example wellbores (Wellbore.WellID).
3. Expand each anchor well into its relationship package via the instance graph.

Returns a connected answer (concepts + anchor wells + graphs) with provenance,
rather than isolated search hits. Read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.connection import ADMEConnection
from app.services.instance_graph import (
    WELLBORE_KIND,
    GraphEdge,
    GraphNode,
    WellGraph,
    build_well_graph,
)
from app.services.marker_search import (
    MarkerHit,
    MarkerSearchConfig,
    MarkerSearchError,
    search_markers,
)
from app.services.search import search_with_cursor
from app.services.semantic_catalog import CatalogHit, search_catalog


class DiscoveryError(RuntimeError):
    """Raised when a combined discovery cannot be completed."""


@dataclass
class DiscoveryResult:
    """The connected answer for a natural-language discovery query."""

    query: str
    concepts: list[MarkerHit] = field(default_factory=list)
    anchor_well_ids: list[str] = field(default_factory=list)
    graphs: list[WellGraph] = field(default_factory=list)
    # marker_name -> anchor well ids it contributed (semantic -> graph bridge)
    concept_wells: dict[str, list[str]] = field(default_factory=dict)
    # Populated when the catalog (multi-entity) source is used.
    catalog_hits: list[CatalogHit] = field(default_factory=list)
    api_calls: int = 0

    @property
    def total_nodes(self) -> int:
        return sum(len(g.nodes) for g in self.graphs)


def _wellbore_to_well(
    connection: ADMEConnection, token: str, wellbore_id: str
) -> str | None:
    """Resolve a wellbore's parent well via its explicit ``WellID``."""
    clean = wellbore_id.rstrip(":")
    page = search_with_cursor(
        connection,
        token,
        kind=WELLBORE_KIND,
        query=f'id:"{clean}"',
        limit=1,
        returned_fields=("id", "data.WellID"),
    )
    if not page.ok:
        return None
    for record in (page.raw_response or {}).get("results", []):
        data = record.get("data") if isinstance(record, dict) else None
        if isinstance(data, dict):
            well_id = data.get("WellID")
            if isinstance(well_id, str) and well_id:
                return well_id.rstrip(":")
    return None


def resolve_anchor_wells(
    connection: ADMEConnection,
    token: str,
    concepts: list[MarkerHit],
    *,
    max_wells: int,
) -> tuple[list[str], dict[str, list[str]], int]:
    """Resolve distinct anchor wells from concepts' example wellbores.

    Returns ``(ordered_well_ids, concept_name -> well_ids, resolution_calls)``.
    The mapping preserves the semantic->graph bridge (which matched concept
    anchored which well).
    """
    wellbore_to_well: dict[str, str | None] = {}
    well_ids: list[str] = []
    concept_wells: dict[str, list[str]] = {}
    calls = 0
    for concept in concepts:
        contributed: list[str] = []
        for wellbore_id in concept.example_wellbores:
            clean = wellbore_id.rstrip(":")
            if clean not in wellbore_to_well:
                wellbore_to_well[clean] = _wellbore_to_well(connection, token, clean)
                calls += 1
            well_id = wellbore_to_well[clean]
            if not well_id:
                continue
            if well_id not in well_ids:
                if len(well_ids) >= max_wells:
                    continue
                well_ids.append(well_id)
            if well_id not in contributed:
                contributed.append(well_id)
        if contributed:
            concept_wells[concept.marker_name] = contributed
    return well_ids, concept_wells, calls


def discover(
    marker_config: MarkerSearchConfig,
    connection: ADMEConnection,
    token: str,
    query: str,
    *,
    top_concepts: int = 5,
    max_wells: int = 3,
) -> DiscoveryResult:
    """Run the combined semantic -> graph discovery for a query."""
    if not query or not query.strip():
        return DiscoveryResult(query=query)

    try:
        concepts = search_markers(marker_config, query, top=top_concepts)
    except MarkerSearchError as exc:
        raise DiscoveryError(f"Semantic interpretation failed: {exc}") from exc

    well_ids, concept_wells, resolve_calls = resolve_anchor_wells(
        connection, token, concepts, max_wells=max_wells
    )

    graphs: list[WellGraph] = []
    graph_calls = 0
    for well_id in well_ids:
        graph = build_well_graph(connection, token, well_id, label="Well")
        graphs.append(graph)
        graph_calls += graph.api_calls

    return DiscoveryResult(
        query=query,
        concepts=concepts,
        anchor_well_ids=well_ids,
        graphs=graphs,
        concept_wells=concept_wells,
        api_calls=resolve_calls + graph_calls,
    )


def _anchor_well_for_hit(
    connection: ADMEConnection, token: str, hit: CatalogHit
) -> tuple[str | None, int]:
    """Resolve a catalog hit to an anchor well id; returns (well_id, calls)."""
    if hit.anchor_well_id:
        return hit.anchor_well_id.rstrip(":"), 0
    if hit.anchor_wellbore_id:
        well_id = _wellbore_to_well(connection, token, hit.anchor_wellbore_id)
        return well_id, 1
    return None, 0


def discover_catalog(
    catalog_config: MarkerSearchConfig,
    connection: ADMEConnection,
    token: str,
    query: str,
    *,
    top_hits: int = 6,
    max_wells: int = 3,
    sources: list[str] | None = None,
) -> DiscoveryResult:
    """Combined discovery over the general catalog (documents, wells, ...).

    Interprets the query across every summarized entity source, resolves each
    hit to an anchor well (directly for wells, via Wellbore.WellID otherwise),
    and expands those wells in the graph. The semantic->graph bridge keys on
    each hit's title (e.g. a report name).
    """
    if not query or not query.strip():
        return DiscoveryResult(query=query)

    try:
        hits = search_catalog(
            catalog_config, query, top=top_hits, sources=sources
        )
    except MarkerSearchError as exc:
        raise DiscoveryError(f"Catalog interpretation failed: {exc}") from exc

    well_ids: list[str] = []
    concept_wells: dict[str, list[str]] = {}
    resolve_calls = 0
    for hit in hits:
        well_id, calls = _anchor_well_for_hit(connection, token, hit)
        resolve_calls += calls
        if not well_id:
            continue
        if well_id not in well_ids:
            if len(well_ids) >= max_wells:
                continue
            well_ids.append(well_id)
        contributed = concept_wells.setdefault(hit.title, [])
        if well_id not in contributed:
            contributed.append(well_id)

    graphs: list[WellGraph] = []
    graph_calls = 0
    for well_id in well_ids:
        graph = build_well_graph(connection, token, well_id, label="Well")
        graphs.append(graph)
        graph_calls += graph.api_calls

    return DiscoveryResult(
        query=query,
        anchor_well_ids=well_ids,
        graphs=graphs,
        concept_wells=concept_wells,
        catalog_hits=hits,
        api_calls=resolve_calls + graph_calls,
    )


def discover_unified(
    marker_config: MarkerSearchConfig,
    catalog_config: MarkerSearchConfig,
    connection: ADMEConnection,
    token: str,
    query: str,
    *,
    top_results: int = 5,
    max_wells: int = 3,
) -> DiscoveryResult:
    """Answer an open question across all sources, then expand the anchors.

    Convenience wrapper: :func:`interpret_unified` to find and rank anchor
    wells, then :func:`expand_wells` to build their graphs. The page uses the
    two steps separately so graph expansion can be tuned *after* results show.
    """
    result = interpret_unified(
        marker_config,
        catalog_config,
        connection,
        token,
        query,
        top_results=top_results,
        max_candidates=max_wells,
    )
    graphs, graph_calls = expand_wells(
        connection, token, result.anchor_well_ids[:max_wells]
    )
    result.graphs = graphs
    result.api_calls += graph_calls
    return result


def interpret_unified(
    marker_config: MarkerSearchConfig,
    catalog_config: MarkerSearchConfig,
    connection: ADMEConnection,
    token: str,
    query: str,
    *,
    top_results: int = 6,
    max_candidates: int = 10,
) -> DiscoveryResult:
    """Interpret an open question and rank candidate anchor wells (no graph).

    Interprets the query against both the stratigraphy marker index and the
    general multi-entity catalog, then resolves ranked candidate anchor wells.
    Graph expansion is deferred to :func:`expand_wells` so the UI can let the
    user choose how many anchors to expand *after* seeing the results.
    """
    if not query or not query.strip():
        return DiscoveryResult(query=query)

    concepts: list[MarkerHit] = []
    catalog_hits: list[CatalogHit] = []
    errors: list[str] = []
    try:
        concepts = search_markers(marker_config, query, top=top_results)
    except MarkerSearchError as exc:
        errors.append(f"markers: {exc}")
    try:
        catalog_hits = search_catalog(catalog_config, query, top=top_results)
    except MarkerSearchError as exc:
        errors.append(f"catalog: {exc}")

    if not concepts and not catalog_hits:
        if errors:
            raise DiscoveryError("; ".join(errors))
        return DiscoveryResult(query=query)

    well_ids: list[str] = []
    concept_wells: dict[str, list[str]] = {}
    calls = 0

    def _add(label: str, well_id: str | None) -> None:
        if not well_id:
            return
        if well_id not in well_ids:
            if len(well_ids) >= max_candidates:
                return
            well_ids.append(well_id)
        contributed = concept_wells.setdefault(label, [])
        if well_id not in contributed:
            contributed.append(well_id)

    # Markers -> anchor wells via each concept's example wellbores (cached).
    wellbore_cache: dict[str, str | None] = {}
    for concept in concepts:
        for wellbore_id in concept.example_wellbores:
            clean = wellbore_id.rstrip(":")
            if clean not in wellbore_cache:
                wellbore_cache[clean] = _wellbore_to_well(
                    connection, token, clean
                )
                calls += 1
            _add(concept.marker_name, wellbore_cache[clean])

    # Catalog -> anchor wells (well hit = itself; else via Wellbore.WellID).
    for hit in catalog_hits:
        well_id, hit_calls = _anchor_well_for_hit(connection, token, hit)
        calls += hit_calls
        _add(hit.title, well_id)

    return DiscoveryResult(
        query=query,
        concepts=concepts,
        catalog_hits=catalog_hits,
        anchor_well_ids=well_ids,
        concept_wells=concept_wells,
        api_calls=calls,
    )


def expand_wells(
    connection: ADMEConnection,
    token: str,
    well_ids: list[str],
) -> tuple[list[WellGraph], int]:
    """Build the relationship graph for each well; return (graphs, api_calls)."""
    graphs: list[WellGraph] = []
    calls = 0
    for well_id in well_ids:
        graph = build_well_graph(connection, token, well_id, label="Well")
        graphs.append(graph)
        calls += graph.api_calls
    return graphs, calls


def merge_discovery_graph(result: DiscoveryResult) -> WellGraph:
    """Merge concepts + anchor well packages into one combined graph.

    Concept nodes (role ``Concept``) link via ``matches`` edges to the anchor
    wells they resolved, so a single view shows the full semantic->graph
    picture: query concepts -> wells -> wellbores -> components.
    """
    merged = WellGraph(label="Discovery", well_id=result.query)
    seen_nodes: set[str] = set()

    def _add_node(node_id: str, role: str) -> None:
        if node_id in seen_nodes:
            return
        seen_nodes.add(node_id)
        merged.nodes.append(GraphNode(id=node_id, role=role))

    for graph in result.graphs:
        for node in graph.nodes:
            _add_node(node.id, node.role)
        merged.edges.extend(graph.edges)

    # Only bridge concepts to wells that are actually expanded (present as
    # nodes), so partial expansion never creates phantom well nodes.
    for marker_name, well_ids in result.concept_wells.items():
        linked = [w for w in well_ids if w in seen_nodes]
        if not linked:
            continue
        concept_id = f"concept::{marker_name}"
        _add_node(concept_id, "Concept")
        for well_id in linked:
            merged.edges.append(
                GraphEdge(concept_id, well_id, "matches", "semantic")
            )

    return merged
