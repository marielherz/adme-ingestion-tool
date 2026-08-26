#!/usr/bin/env python
"""Hybrid semantic query over the ADME marker vocabulary index.

Embeds the query with the Foundry deployment, then runs a hybrid search
(keyword BM25 + vector KNN) with the semantic ranker on Azure AI Search.

Config via environment:
- ``SEARCH_ENDPOINT`` / ``SEARCH_QUERY_KEY`` (or ``SEARCH_ADMIN_KEY``)
- ``SEARCH_INDEX_NAME`` (default: ``adme-markers``)
- ``AZURE_OPENAI_ENDPOINT`` / ``AZURE_OPENAI_API_KEY`` / ``AZURE_OPENAI_DEPLOYMENT``

Usage::

    python scripts/query_markers.py "sandstone reservoir formations"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.services.marker_search import (  # noqa: E402
    DEFAULT_INDEX_NAME,
    MarkerSearchConfig,
    MarkerSearchError,
    search_markers,
)


def _config_from_env() -> MarkerSearchConfig:
    return MarkerSearchConfig(
        search_endpoint=os.getenv("SEARCH_ENDPOINT", ""),
        search_key=os.getenv("SEARCH_QUERY_KEY") or os.getenv("SEARCH_ADMIN_KEY", ""),
        foundry_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
        foundry_key=os.getenv("AZURE_OPENAI_API_KEY", ""),
        index_name=os.getenv("SEARCH_INDEX_NAME", DEFAULT_INDEX_NAME),
        deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT", "text-embedding-3-small"),
    )


def main() -> int:
    query_text = " ".join(sys.argv[1:]).strip() or "sandstone reservoir formations"
    config = _config_from_env()
    if not config.is_complete():
        print(
            "[ERROR] Set SEARCH_ENDPOINT, SEARCH_QUERY_KEY (or SEARCH_ADMIN_KEY), "
            "AZURE_OPENAI_ENDPOINT, and AZURE_OPENAI_API_KEY."
        )
        return 1

    try:
        hits = search_markers(config, query_text, top=10)
    except MarkerSearchError as exc:
        print(f"[ERROR] {exc}")
        return 1

    print(f"\nQuery: {query_text!r}\n" + "-" * 60)
    if not hits:
        print("  (no results)")
        return 0

    for hit in hits:
        ages = ", ".join(hit.geological_ages) or "-"
        if hit.depth_min is not None and hit.depth_max is not None:
            depth = f"{hit.depth_min:.0f}-{hit.depth_max:.0f}m"
        else:
            depth = "-"
        print(
            f"  [{hit.rank_score:5.2f}]  {hit.marker_name:<40} "
            f"wells={hit.wellbore_count:<4} picks={hit.occurrence_count:<4} "
            f"age={ages}  depth={depth}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
