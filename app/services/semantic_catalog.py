"""Generalized multi-entity semantic catalog.

The marker vocabulary index answers *stratigraphy* questions; markers are one
entity source. This module generalizes semantic search to **any OSDU entity**:
each record is summarized into a :class:`SemanticDocument` (title + embeddable
content + anchor ids) and indexed in a single Azure AI Search index
(``adme-catalog``) with a ``source`` facet. Discovery can then interpret a
query across documents/reports, wells, wellbores, etc., and resolve the anchor
record of *any* kind before expanding it in the graph.

This keeps the marker index intact (it stays specialized) while adding a
parallel, general catalog — e.g. so a query like *"shallow gas drilling
hazard"* matches a **well report narrative**, not just a marker name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from azure.search.documents.models import VectorizedQuery

from app.services.marker_search import MarkerSearchConfig, MarkerSearchError
from app.services.semantic_embeddings import OpenAIEmbedding

CATALOG_INDEX_NAME = "adme-catalog"
CATALOG_SEMANTIC_CONFIG = "catalog-semantic"
CATALOG_VECTOR_PROFILE = "catalog-hnsw"
CATALOG_HNSW_ALGO = "catalog-hnsw-algo"
EMBED_DIM = 1536

# OSDU kinds we know how to summarize -> source label.
DOCUMENT_KIND = "osdu:wks:work-product-component--Document:1.0.0"
WELL_KIND = "osdu:wks:master-data--Well:1.0.0"
WELLBORE_KIND = "osdu:wks:master-data--Wellbore:1.0.0"

_KEY_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-=]")


def _doc_key(record_id: str, source: str) -> str:
    """Return an AI Search key-safe id (keys forbid ``:`` / ``.`` / ``/``)."""
    return f"{source}__{_KEY_SAFE_RE.sub('_', record_id)}"


def _strip_ref(value: Any) -> str | None:
    """Normalize an OSDU relationship reference to a bare record id."""
    if isinstance(value, str) and value:
        return value.rstrip(":")
    return None


@dataclass(frozen=True)
class SemanticDocument:
    """One embeddable entity summary for the general catalog."""

    record_id: str
    source: str  # document | well | wellbore | marker | ...
    kind: str
    title: str
    content: str
    anchor_well_id: str | None = None
    anchor_wellbore_id: str | None = None

    @property
    def key(self) -> str:
        return _doc_key(self.record_id, self.source)

    def to_index_doc(self, vector: list[float]) -> dict[str, Any]:
        return {
            "id": self.key,
            "recordId": self.record_id,
            "source": self.source,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "anchorWellId": self.anchor_well_id or "",
            "anchorWellboreId": self.anchor_wellbore_id or "",
            "contentVector": vector,
        }


@dataclass(frozen=True)
class CatalogHit:
    """A ranked catalog result."""

    record_id: str
    source: str
    kind: str
    title: str
    content: str
    anchor_well_id: str | None
    anchor_wellbore_id: str | None
    score: float | None
    reranker_score: float | None

    @property
    def rank_score(self) -> float:
        if self.reranker_score is not None:
            return self.reranker_score
        return self.score or 0.0


# ---------------------------------------------------------------------------
# Summarizers: OSDU record -> SemanticDocument
# ---------------------------------------------------------------------------


def _data(record: dict[str, Any]) -> dict[str, Any]:
    data = record.get("data")
    return data if isinstance(data, dict) else {}


def document_to_semantic(record: dict[str, Any]) -> SemanticDocument | None:
    """Summarize a Document WPC (report/narrative) for the catalog."""
    record_id = record.get("id")
    if not isinstance(record_id, str):
        return None
    data = _data(record)
    name = str(data.get("Name") or "").strip()
    description = str(data.get("Description") or "").strip()
    title = name or "Document"
    content = "\n".join(part for part in (name, description) if part) or title
    return SemanticDocument(
        record_id=record_id,
        source="document",
        kind=str(record.get("kind") or DOCUMENT_KIND),
        title=title,
        content=content,
        anchor_wellbore_id=_strip_ref(data.get("WellboreID")),
    )


def _name_aliases(data: dict[str, Any]) -> list[str]:
    aliases = data.get("NameAliases")
    if not isinstance(aliases, list):
        return []
    out = []
    for item in aliases:
        if isinstance(item, dict):
            name = item.get("AliasName")
            if isinstance(name, str) and name:
                out.append(name)
    return out


def well_to_semantic(record: dict[str, Any]) -> SemanticDocument | None:
    """Summarize a Well master-data record for the catalog."""
    record_id = record.get("id")
    if not isinstance(record_id, str):
        return None
    data = _data(record)
    name = str(data.get("FacilityName") or "").strip()
    title = name or record_id.split(":")[-1]
    parts = [name, *_name_aliases(data)]
    description = str(data.get("Description") or "").strip()
    if description:
        parts.append(description)
    content = "\n".join(p for p in parts if p) or title
    return SemanticDocument(
        record_id=record_id,
        source="well",
        kind=str(record.get("kind") or WELL_KIND),
        title=title,
        content=content,
        anchor_well_id=record_id.rstrip(":"),
    )


def wellbore_to_semantic(record: dict[str, Any]) -> SemanticDocument | None:
    """Summarize a Wellbore master-data record for the catalog."""
    record_id = record.get("id")
    if not isinstance(record_id, str):
        return None
    data = _data(record)
    name = str(data.get("FacilityName") or "").strip()
    title = name or record_id.split(":")[-1]
    parts = [name, *_name_aliases(data)]
    remark = str(data.get("Remark") or "").strip()
    if remark:
        parts.append(remark)
    content = "\n".join(p for p in parts if p) or title
    return SemanticDocument(
        record_id=record_id,
        source="wellbore",
        kind=str(record.get("kind") or WELLBORE_KIND),
        title=title,
        content=content,
        anchor_well_id=_strip_ref(data.get("WellID")),
        anchor_wellbore_id=record_id.rstrip(":"),
    )


# Map summarizable kinds (by short entity segment) to their summarizer.
_SUMMARIZERS = {
    "work-product-component--Document": document_to_semantic,
    "master-data--Well": well_to_semantic,
    "master-data--Wellbore": wellbore_to_semantic,
}


def summarize_record(record: dict[str, Any]) -> SemanticDocument | None:
    """Dispatch a raw ADME record to the matching summarizer, if any."""
    kind = record.get("kind")
    if not isinstance(kind, str):
        return None
    parts = kind.split(":")
    entity = parts[2] if len(parts) >= 3 else ""
    summarizer = _SUMMARIZERS.get(entity)
    return summarizer(record) if summarizer else None


# ---------------------------------------------------------------------------
# Index schema + search
# ---------------------------------------------------------------------------


def build_catalog_index(index_name: str = CATALOG_INDEX_NAME) -> SearchIndex:
    """Return the multi-entity catalog index definition."""
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(
            name="recordId", type=SearchFieldDataType.String, filterable=True
        ),
        SimpleField(
            name="source",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
        SimpleField(
            name="kind", type=SearchFieldDataType.String, filterable=True
        ),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SimpleField(
            name="anchorWellId",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(
            name="anchorWellboreId",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SearchField(
            name="contentVector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBED_DIM,
            vector_search_profile_name=CATALOG_VECTOR_PROFILE,
        ),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name=CATALOG_HNSW_ALGO)],
        profiles=[
            VectorSearchProfile(
                name=CATALOG_VECTOR_PROFILE,
                algorithm_configuration_name=CATALOG_HNSW_ALGO,
            )
        ],
    )
    semantic_search = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name=CATALOG_SEMANTIC_CONFIG,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[SemanticField(field_name="content")],
                    keywords_fields=[SemanticField(field_name="source")],
                ),
            )
        ]
    )
    return SearchIndex(
        name=index_name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )


def _embed_query(config: MarkerSearchConfig, query_text: str) -> list[float]:
    embedder = OpenAIEmbedding(
        model=config.deployment,
        api_key=config.foundry_key,
        endpoint=config.foundry_endpoint,
    )
    return embedder.embed(query_text)


def _to_hit(result: dict[str, Any]) -> CatalogHit:
    return CatalogHit(
        record_id=result.get("recordId", ""),
        source=result.get("source", ""),
        kind=result.get("kind", ""),
        title=result.get("title", ""),
        content=result.get("content", ""),
        anchor_well_id=result.get("anchorWellId") or None,
        anchor_wellbore_id=result.get("anchorWellboreId") or None,
        score=result.get("@search.score"),
        reranker_score=result.get("@search.reranker_score"),
    )


def search_catalog(
    config: MarkerSearchConfig,
    query_text: str,
    *,
    top: int = 6,
    sources: list[str] | None = None,
) -> list[CatalogHit]:
    """Hybrid (keyword + vector + semantic) search over the general catalog."""
    if not query_text or not query_text.strip():
        return []
    client = SearchClient(
        endpoint=config.search_endpoint,
        index_name=config.index_name or CATALOG_INDEX_NAME,
        credential=AzureKeyCredential(config.search_key),
    )
    vector = _embed_query(config, query_text)
    vector_query = VectorizedQuery(
        vector=vector,
        k_nearest_neighbors=top,
        fields="contentVector",
    )
    search_filter = None
    if sources:
        joined = ",".join(sources)
        search_filter = f"search.in(source, '{joined}', ',')"
    try:
        results = client.search(
            search_text=query_text,
            vector_queries=[vector_query],
            query_type="semantic",
            semantic_configuration_name=CATALOG_SEMANTIC_CONFIG,
            filter=search_filter,
            select=[
                "recordId",
                "source",
                "kind",
                "title",
                "content",
                "anchorWellId",
                "anchorWellboreId",
            ],
            top=top,
        )
        return [_to_hit(r) for r in results]
    except Exception as exc:  # noqa: BLE001 - surface any query failure
        raise MarkerSearchError(f"Catalog search failed: {exc}") from exc


@dataclass
class CatalogBuildResult:
    documents: list[SemanticDocument] = field(default_factory=list)
    skipped: int = 0


def summarize_records(records: list[dict[str, Any]]) -> CatalogBuildResult:
    """Summarize a batch of raw ADME records into catalog documents."""
    docs: list[SemanticDocument] = []
    skipped = 0
    for record in records:
        doc = summarize_record(record)
        if doc is None:
            skipped += 1
        else:
            docs.append(doc)
    return CatalogBuildResult(documents=docs, skipped=skipped)
