"""Session-scoped semantic connection settings.

Holds the Azure AI Search + Foundry configuration that powers Intelligent
Discovery (semantic search, catalog, and answer synthesis). Kept separate from
the page code so the **Semantic Connection** setup blade and the **Intelligent
Discovery** page share one source of truth: the blade edits it, Discovery reads
it and checks it as a pre-req.

Secrets (search/Foundry keys) live in session state only and are never written
to disk. Defaults resolve from environment variables when present.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from typing import Any

from app.services.marker_search import DEFAULT_INDEX_NAME, MarkerSearchConfig
from app.services.semantic_catalog import CATALOG_INDEX_NAME

SEMANTIC_SETTINGS_KEY = "semantic_settings"

DEFAULT_SEARCH_ENDPOINT = "https://adme-semantic-search-mh.search.windows.net"
DEFAULT_FOUNDRY_ENDPOINT = "https://marielfoundry.services.ai.azure.com"
DEFAULT_DEPLOYMENT = "text-embedding-3-small"


@dataclass
class SemanticSettings:
    """Everything Intelligent Discovery needs to reach Search + Foundry."""

    search_endpoint: str = ""
    search_key: str = ""
    foundry_endpoint: str = ""
    foundry_key: str = ""
    marker_index: str = DEFAULT_INDEX_NAME
    catalog_index: str = CATALOG_INDEX_NAME
    deployment: str = DEFAULT_DEPLOYMENT
    chat_deployment: str = ""

    def missing_fields(self) -> list[str]:
        """Return the labels of required fields that are not yet set."""
        required = {
            "Search endpoint": self.search_endpoint,
            "Search key": self.search_key,
            "Foundry endpoint": self.foundry_endpoint,
            "Foundry key": self.foundry_key,
            "Marker index": self.marker_index,
            "Embedding deployment": self.deployment,
        }
        return [label for label, value in required.items() if not value.strip()]

    def is_complete(self) -> bool:
        return not self.missing_fields()

    def has_chat(self) -> bool:
        return bool(self.chat_deployment.strip())

    def marker_config(self) -> MarkerSearchConfig:
        return MarkerSearchConfig(
            search_endpoint=self.search_endpoint.strip(),
            search_key=self.search_key.strip(),
            foundry_endpoint=self.foundry_endpoint.strip(),
            foundry_key=self.foundry_key.strip(),
            index_name=self.marker_index.strip() or DEFAULT_INDEX_NAME,
            deployment=self.deployment.strip() or DEFAULT_DEPLOYMENT,
        )

    def catalog_config(self) -> MarkerSearchConfig:
        return dc_replace(
            self.marker_config(),
            index_name=self.catalog_index.strip() or CATALOG_INDEX_NAME,
        )


def default_semantic_settings() -> SemanticSettings:
    """Build settings from environment variables (with sensible fallbacks)."""
    return SemanticSettings(
        search_endpoint=os.getenv("SEARCH_ENDPOINT", DEFAULT_SEARCH_ENDPOINT),
        search_key=os.getenv("SEARCH_QUERY_KEY") or os.getenv("SEARCH_ADMIN_KEY", ""),
        foundry_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", DEFAULT_FOUNDRY_ENDPOINT),
        foundry_key=os.getenv("AZURE_OPENAI_API_KEY", ""),
        marker_index=os.getenv("SEARCH_INDEX_NAME", DEFAULT_INDEX_NAME),
        catalog_index=os.getenv("SEARCH_CATALOG_INDEX_NAME", CATALOG_INDEX_NAME),
        deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT", DEFAULT_DEPLOYMENT),
        chat_deployment=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", ""),
    )


def get_semantic_settings(
    session_state: MutableMapping[str, Any],
) -> SemanticSettings:
    """Return the session's settings, seeding from environment on first use."""
    existing = session_state.get(SEMANTIC_SETTINGS_KEY)
    if isinstance(existing, SemanticSettings):
        return existing
    settings = default_semantic_settings()
    session_state[SEMANTIC_SETTINGS_KEY] = settings
    return settings


def set_semantic_settings(
    session_state: MutableMapping[str, Any], settings: SemanticSettings
) -> None:
    """Persist settings into session state."""
    session_state[SEMANTIC_SETTINGS_KEY] = settings
