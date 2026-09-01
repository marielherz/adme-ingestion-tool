#!/usr/bin/env python
"""Extract stratigraphic marker vocabulary from ADME and index it in Azure AI Search.

Pipeline (quick-win "direction B"):
1. Page all WellboreMarkerSet WPC records from the ADME Search API.
2. Aggregate distinct marker names with cross-well statistics.
3. Embed each term with the Foundry ``text-embedding-3-small`` deployment.
4. Create (or update) an Azure AI Search index with a vector + semantic config.
5. Upload the documents.
6. Run a sample hybrid (keyword + vector + semantic) query.

Auth/config via environment (no secrets are printed or stored in code):
- ``az login`` provides the ADME token (resource https://energy.azure.com).
- ``AZURE_OPENAI_ENDPOINT`` / ``AZURE_OPENAI_DEPLOYMENT`` / ``AZURE_OPENAI_API_KEY``
  for embeddings (same Foundry deployment used elsewhere).
- ``SEARCH_ENDPOINT`` / ``SEARCH_ADMIN_KEY`` for Azure AI Search.
- ``SEARCH_INDEX_NAME`` (default: ``adme-markers``).
- ``MARKER_MAX_SETS`` optional cap for a fast dry run.

Usage::

    az login
    python scripts/index_markers.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from azure.core.credentials import AzureKeyCredential  # noqa: E402
from azure.search.documents import SearchClient  # noqa: E402
from azure.search.documents.indexes import SearchIndexClient  # noqa: E402
from azure.search.documents.indexes.models import (  # noqa: E402
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

from app.services.auth import acquire_cli_token  # noqa: E402
from app.services.marker_vocabulary import (  # noqa: E402
    MarkerVocabularyEntry,
    aggregate_markers,
)
from app.services.search import search_with_cursor  # noqa: E402
from app.services.semantic_embeddings import OpenAIEmbedding  # noqa: E402
from scripts.sample_wpc_text import _connection  # noqa: E402

MARKERSET_KIND = "osdu:wks:work-product-component--WellboreMarkerSet:1.0.0"
EMBED_DIM = 1536
VECTOR_PROFILE = "marker-hnsw"
SEMANTIC_CONFIG = "marker-semantic"


def _adme_token(connection) -> str:
    return acquire_cli_token(resource=connection.scope.removesuffix("/.default"))


def _pull_markerset_records(connection, token, *, max_sets: int | None) -> list[dict]:
    """Page all WellboreMarkerSet records (marker subfields) from ADME Search.

    A single slow Elasticsearch page can exceed the client timeout, so each
    page is retried a few times before giving up. The cursor is unchanged on
    retry, so no records are skipped or duplicated.
    """
    records: list[dict] = []
    cursor: str | None = None
    fields = (
        "id",
        "data.WellboreID",
        "data.Markers.MarkerName",
        "data.Markers.GeologicalAge",
        "data.Markers.MarkerMeasuredDepth",
        "data.Markers.MarkerInterpreter",
    )
    max_retries = 4
    while True:
        page = None
        for attempt in range(1, max_retries + 1):
            page = search_with_cursor(
                connection,
                token,
                kind=MARKERSET_KIND,
                limit=100,
                cursor=cursor,
                returned_fields=fields,
            )
            if page.ok:
                break
            print(
                f"\n  page retry {attempt}/{max_retries} "
                f"(HTTP {page.http_status}: {page.error_message})"
            )
        if page is None or not page.ok:
            raise RuntimeError(
                f"Marker search failed after {max_retries} retries: "
                f"{page.error_message if page else 'no response'}"
            )
        raw = page.raw_response if isinstance(page.raw_response, dict) else {}
        batch = raw.get("results") or []
        records.extend(batch)
        print(f"  pulled {len(records)} marker sets...", end="\r")
        if max_sets is not None and len(records) >= max_sets:
            records = records[:max_sets]
            break
        if not page.has_more:
            break
        cursor = page.cursor
    print()
    return records


def _build_index(index_name: str) -> SearchIndex:
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="markerName", type=SearchFieldDataType.String),
        SearchableField(name="searchText", type=SearchFieldDataType.String),
        SimpleField(
            name="normalizedName",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SearchField(
            name="geologicalAges",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            searchable=True,
            filterable=True,
            facetable=True,
        ),
        SimpleField(
            name="occurrenceCount",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),
        SimpleField(
            name="wellboreCount",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),
        SimpleField(name="depthMin", type=SearchFieldDataType.Double, filterable=True),
        SimpleField(name="depthMax", type=SearchFieldDataType.Double, filterable=True),
        SearchField(
            name="exampleWellbores",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
        ),
        SearchField(
            name="contentVector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBED_DIM,
            vector_search_profile_name=VECTOR_PROFILE,
        ),
    ]

    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="marker-hnsw-algo")],
        profiles=[
            VectorSearchProfile(
                name=VECTOR_PROFILE,
                algorithm_configuration_name="marker-hnsw-algo",
            )
        ],
    )

    semantic_search = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIG,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="markerName"),
                    content_fields=[SemanticField(field_name="searchText")],
                    keywords_fields=[SemanticField(field_name="geologicalAges")],
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


def _to_documents(
    entries: list[MarkerVocabularyEntry],
    embeddings: list[list[float]],
) -> list[dict]:
    docs = []
    for entry, vector in zip(entries, embeddings):
        if not vector:
            continue
        docs.append(
            {
                "id": entry.id,
                "markerName": entry.marker_name,
                "searchText": entry.to_search_text(),
                "normalizedName": entry.normalized_name,
                "geologicalAges": entry.geological_ages,
                "occurrenceCount": entry.occurrence_count,
                "wellboreCount": entry.wellbore_count,
                "depthMin": entry.depth_min,
                "depthMax": entry.depth_max,
                "exampleWellbores": entry.example_wellbores,
                "contentVector": vector,
            }
        )
    return docs


def main() -> int:
    search_endpoint = os.getenv("SEARCH_ENDPOINT")
    search_key = os.getenv("SEARCH_ADMIN_KEY")
    index_name = os.getenv("SEARCH_INDEX_NAME", "adme-markers")
    if not search_endpoint or not search_key:
        print("[ERROR] Set SEARCH_ENDPOINT and SEARCH_ADMIN_KEY.")
        return 1
    if not (os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY")):
        print("[ERROR] Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY.")
        return 1

    os.environ.setdefault("ADME_TOKEN_SCOPE", "https://energy.azure.com/.default")
    max_sets_env = os.getenv("MARKER_MAX_SETS")
    max_sets = int(max_sets_env) if max_sets_env else None

    connection = _connection()
    print("ADME marker vocabulary indexer")
    print("=" * 60)
    print(f"ADME:   {connection.endpoint} ({connection.data_partition_id})")
    print(f"Search: {search_endpoint} / index '{index_name}'")

    token = _adme_token(connection)
    print("\nPulling WellboreMarkerSet records...")
    records = _pull_markerset_records(connection, token, max_sets=max_sets)
    print(f"Pulled {len(records)} marker sets.")

    entries = aggregate_markers(records)
    print(f"Aggregated {len(entries)} distinct marker terms.")
    if not entries:
        print("[INFO] Nothing to index.")
        return 0

    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "text-embedding-3-small")
    embedder = OpenAIEmbedding(model=deployment)
    print(f"Embedding {len(entries)} terms via '{deployment}'...")
    texts = [e.to_search_text() for e in entries]
    vectors: list[list[float]] = []
    batch = 256
    for i in range(0, len(texts), batch):
        vectors.extend(embedder.embed_batch(texts[i : i + batch]))
        print(f"  embedded {min(i + batch, len(texts))}/{len(texts)}", end="\r")
    print()

    index_client = SearchIndexClient(search_endpoint, AzureKeyCredential(search_key))
    index_client.create_or_update_index(_build_index(index_name))
    print(f"Index '{index_name}' created/updated.")

    docs = _to_documents(entries, vectors)
    search_client = SearchClient(
        search_endpoint, index_name, AzureKeyCredential(search_key)
    )
    uploaded = 0
    for i in range(0, len(docs), 1000):
        result = search_client.upload_documents(docs[i : i + 1000])
        uploaded += sum(1 for r in result if r.succeeded)
    print(f"Uploaded {uploaded}/{len(docs)} documents.")

    print("\nTop 10 terms by occurrence:")
    for e in entries[:10]:
        ages = ", ".join(e.geological_ages) or "-"
        print(
            f"  {e.occurrence_count:>4}x  {e.marker_name:<38} "
            f"wells={e.wellbore_count:<4} age={ages}"
        )

    print("\n[COMPLETE] Marker vocabulary indexed. Query with scripts/query_markers.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
