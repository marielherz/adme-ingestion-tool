#!/usr/bin/env python
"""Build the generalized multi-entity semantic catalog (``adme-catalog``).

Pulls summarizable OSDU records (Documents/reports, Wells, Wellbores) from the
ADME Search API, summarizes each into a :class:`SemanticDocument`, embeds the
text with the Foundry deployment, and (re)builds the ``adme-catalog`` Azure AI
Search index. This generalizes semantic search beyond the marker vocabulary so
queries can match report narratives, well descriptions, etc.

Auth/config via environment (no secrets printed or stored):
- ``az login`` provides the ADME token (resource https://energy.azure.com).
- ``AZURE_OPENAI_ENDPOINT`` / ``AZURE_OPENAI_DEPLOYMENT`` / ``AZURE_OPENAI_API_KEY``.
- ``SEARCH_ENDPOINT`` / ``SEARCH_ADMIN_KEY``.
- ``SEARCH_INDEX_NAME`` (default: ``adme-catalog``).
- ``CATALOG_MAX_PER_KIND`` optional cap per kind (default: 3000).

Usage::

    az login
    python scripts/index_catalog.py
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

from app.services.auth import acquire_cli_token  # noqa: E402
from app.services.search import search_with_cursor  # noqa: E402
from app.services.semantic_catalog import (  # noqa: E402
    CATALOG_INDEX_NAME,
    SemanticDocument,
    build_catalog_index,
    summarize_records,
)
from app.services.semantic_embeddings import OpenAIEmbedding  # noqa: E402
from scripts.sample_wpc_text import _connection  # noqa: E402

# (kind, returned_fields) for each summarizable source.
_SOURCES = [
    (
        "osdu:wks:work-product-component--Document:1.0.0",
        ("id", "kind", "data.Name", "data.Description", "data.WellboreID"),
    ),
    (
        "osdu:wks:master-data--Well:1.0.0",
        ("id", "kind", "data.FacilityName", "data.NameAliases", "data.Description"),
    ),
    (
        "osdu:wks:master-data--Wellbore:1.0.0",
        (
            "id",
            "kind",
            "data.FacilityName",
            "data.NameAliases",
            "data.Remark",
            "data.WellID",
        ),
    ),
]


def _adme_token(connection) -> str:
    return acquire_cli_token(resource=connection.scope.removesuffix("/.default"))


def _pull_kind(connection, token, kind, fields, *, cap: int) -> list[dict]:
    """Page records of one kind from ADME Search (bounded by ``cap``)."""
    records: list[dict] = []
    cursor: str | None = None
    max_retries = 4
    while True:
        page = None
        for attempt in range(1, max_retries + 1):
            page = search_with_cursor(
                connection,
                token,
                kind=kind,
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
                f"Search failed for {kind} after {max_retries} retries: "
                f"{page.error_message if page else 'no response'}"
            )
        raw = page.raw_response if isinstance(page.raw_response, dict) else {}
        batch = raw.get("results") or []
        records.extend(batch)
        print(f"  {kind.split(':')[2]}: pulled {len(records)}...", end="\r")
        if len(records) >= cap:
            records = records[:cap]
            break
        if not page.has_more:
            break
        cursor = page.cursor
    print()
    return records


def main() -> int:
    search_endpoint = os.getenv("SEARCH_ENDPOINT")
    search_key = os.getenv("SEARCH_ADMIN_KEY")
    index_name = os.getenv("SEARCH_INDEX_NAME", CATALOG_INDEX_NAME)
    if not search_endpoint or not search_key:
        print("[ERROR] Set SEARCH_ENDPOINT and SEARCH_ADMIN_KEY.")
        return 1
    if not (os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY")):
        print("[ERROR] Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY.")
        return 1

    os.environ.setdefault("ADME_TOKEN_SCOPE", "https://energy.azure.com/.default")
    cap = int(os.getenv("CATALOG_MAX_PER_KIND", "3000"))

    connection = _connection()
    print("ADME semantic catalog indexer")
    print("=" * 60)
    print(f"ADME:   {connection.endpoint} ({connection.data_partition_id})")
    print(f"Search: {search_endpoint} / index '{index_name}'")
    print(f"Cap per kind: {cap}")

    token = _adme_token(connection)

    all_docs: list[SemanticDocument] = []
    by_source: dict[str, int] = {}
    print("\nPulling and summarizing records...")
    for kind, fields in _SOURCES:
        records = _pull_kind(connection, token, kind, fields, cap=cap)
        result = summarize_records(records)
        for doc in result.documents:
            by_source[doc.source] = by_source.get(doc.source, 0) + 1
        all_docs.extend(result.documents)
        print(
            f"  {kind.split(':')[2]}: {len(result.documents)} summarized, "
            f"{result.skipped} skipped."
        )

    if not all_docs:
        print("[INFO] Nothing to index.")
        return 0

    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "text-embedding-3-small")
    embedder = OpenAIEmbedding(model=deployment)
    print(f"\nEmbedding {len(all_docs)} documents via '{deployment}'...")
    texts = [d.content for d in all_docs]
    vectors: list[list[float]] = []
    batch = 256
    for i in range(0, len(texts), batch):
        vectors.extend(embedder.embed_batch(texts[i : i + batch]))
        print(f"  embedded {min(i + batch, len(texts))}/{len(texts)}", end="\r")
    print()

    index_client = SearchIndexClient(search_endpoint, AzureKeyCredential(search_key))
    index_client.create_or_update_index(build_catalog_index(index_name))
    print(f"Index '{index_name}' created/updated.")

    docs = [
        doc.to_index_doc(vector)
        for doc, vector in zip(all_docs, vectors)
        if vector
    ]
    search_client = SearchClient(
        search_endpoint, index_name, AzureKeyCredential(search_key)
    )
    uploaded = 0
    for i in range(0, len(docs), 1000):
        result = search_client.upload_documents(docs[i : i + 1000])
        uploaded += sum(1 for r in result if r.succeeded)
    print(f"Uploaded {uploaded}/{len(docs)} documents.")

    print("\nBy source:")
    for source, count in sorted(by_source.items()):
        print(f"  {source:<12} {count}")

    print("\n[COMPLETE] Semantic catalog indexed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
