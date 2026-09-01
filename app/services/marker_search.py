"""Hybrid semantic search over the ADME marker vocabulary index.

Wraps Azure AI Search (keyword + vector + semantic reranking) with Foundry
query embeddings behind a small, testable surface used by both the CLI
(``scripts/query_markers.py``) and the Streamlit Marker Search page.

Configuration is passed explicitly via :class:`MarkerSearchConfig` so callers
control where secrets come from (environment, keyring, or session state). No
secrets are read from the environment inside this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.semantic_embeddings import OpenAIEmbedding

SEMANTIC_CONFIG = "marker-semantic"
DEFAULT_INDEX_NAME = "adme-markers"


class MarkerSearchError(RuntimeError):
    """Raised when a marker search cannot be completed."""


@dataclass(frozen=True)
class MarkerSearchConfig:
    """Everything needed to run a marker search."""

    search_endpoint: str
    search_key: str
    foundry_endpoint: str
    foundry_key: str
    index_name: str = DEFAULT_INDEX_NAME
    deployment: str = "text-embedding-3-small"

    def is_complete(self) -> bool:
        return bool(
            self.search_endpoint
            and self.search_key
            and self.foundry_endpoint
            and self.foundry_key
        )


@dataclass(frozen=True)
class MarkerHit:
    """A single ranked marker vocabulary result."""

    marker_name: str
    geological_ages: list[str]
    occurrence_count: int
    wellbore_count: int
    depth_min: float | None
    depth_max: float | None
    score: float | None
    reranker_score: float | None
    example_wellbores: list[str] = field(default_factory=list)

    @property
    def rank_score(self) -> float:
        """Prefer the semantic reranker score when present."""
        if self.reranker_score is not None:
            return self.reranker_score
        return self.score or 0.0


def embed_query(config: MarkerSearchConfig, query_text: str) -> list[float]:
    """Embed a query string with the configured Foundry deployment."""
    embedder = OpenAIEmbedding(
        model=config.deployment,
        api_key=config.foundry_key,
        endpoint=config.foundry_endpoint,
    )
    return embedder.embed(query_text)


def search_markers(
    config: MarkerSearchConfig,
    query_text: str,
    *,
    top: int = 10,
) -> list[MarkerHit]:
    """Run a hybrid (keyword + vector + semantic) marker search.

    Args:
        config: Search and Foundry connection settings.
        query_text: Natural-language query.
        top: Maximum number of results.

    Returns:
        Ranked marker hits (best first).

    Raises:
        MarkerSearchError: If configuration is incomplete or the query fails.
    """
    if not config.is_complete():
        raise MarkerSearchError("Marker search configuration is incomplete.")
    if not query_text or not query_text.strip():
        return []

    try:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
        from azure.search.documents.models import VectorizedQuery
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise MarkerSearchError(
            "azure-search-documents is required. Install: "
            "pip install azure-search-documents"
        ) from exc

    vector = embed_query(config, query_text)

    client = SearchClient(
        config.search_endpoint,
        config.index_name,
        AzureKeyCredential(config.search_key),
    )
    vector_query = VectorizedQuery(
        vector=vector,
        k_nearest_neighbors=top,
        fields="contentVector",
    )

    try:
        results = client.search(
            search_text=query_text,
            vector_queries=[vector_query],
            query_type="semantic",
            semantic_configuration_name=SEMANTIC_CONFIG,
            select=[
                "markerName",
                "geologicalAges",
                "occurrenceCount",
                "wellboreCount",
                "depthMin",
                "depthMax",
                "exampleWellbores",
            ],
            top=top,
        )
        return [_to_hit(r) for r in results]
    except Exception as exc:  # noqa: BLE001 - surface any query failure
        raise MarkerSearchError(f"Marker search failed: {exc}") from exc


def _to_hit(result: dict) -> MarkerHit:
    return MarkerHit(
        marker_name=result.get("markerName", ""),
        geological_ages=list(result.get("geologicalAges") or []),
        occurrence_count=int(result.get("occurrenceCount") or 0),
        wellbore_count=int(result.get("wellboreCount") or 0),
        depth_min=result.get("depthMin"),
        depth_max=result.get("depthMax"),
        score=result.get("@search.score"),
        reranker_score=result.get("@search.reranker_score"),
        example_wellbores=list(result.get("exampleWellbores") or []),
    )
