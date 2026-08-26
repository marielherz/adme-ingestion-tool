"""Tests for the shared semantic connection settings."""

from __future__ import annotations

from app.semantic_connection import (
    SEMANTIC_SETTINGS_KEY,
    SemanticSettings,
    default_semantic_settings,
    get_semantic_settings,
    set_semantic_settings,
)


def test_missing_fields_and_completeness() -> None:
    empty = SemanticSettings()
    missing = empty.missing_fields()
    assert "Search endpoint" in missing
    assert "Foundry key" in missing
    assert not empty.is_complete()

    full = SemanticSettings(
        search_endpoint="https://s.search.windows.net",
        search_key="k",
        foundry_endpoint="https://f.services.ai.azure.com",
        foundry_key="fk",
        marker_index="adme-markers",
        catalog_index="adme-catalog",
        deployment="text-embedding-3-small",
    )
    assert full.is_complete()
    assert full.missing_fields() == []


def test_marker_and_catalog_configs_share_connection() -> None:
    settings = SemanticSettings(
        search_endpoint="https://s.search.windows.net",
        search_key="k",
        foundry_endpoint="https://f.services.ai.azure.com",
        foundry_key="fk",
        marker_index="adme-markers",
        catalog_index="adme-catalog",
        deployment="text-embedding-3-small",
    )
    marker = settings.marker_config()
    catalog = settings.catalog_config()
    assert marker.index_name == "adme-markers"
    assert catalog.index_name == "adme-catalog"
    # Same connection, only the index differs.
    assert catalog.search_endpoint == marker.search_endpoint
    assert catalog.search_key == marker.search_key
    assert catalog.foundry_endpoint == marker.foundry_endpoint


def test_has_chat_reflects_deployment() -> None:
    assert not SemanticSettings().has_chat()
    assert SemanticSettings(chat_deployment="gpt-4.1-mini").has_chat()


def test_default_settings_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_ENDPOINT", "https://env.search.windows.net")
    monkeypatch.setenv("SEARCH_ADMIN_KEY", "envkey")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4.1-mini")
    settings = default_semantic_settings()
    assert settings.search_endpoint == "https://env.search.windows.net"
    assert settings.search_key == "envkey"
    assert settings.chat_deployment == "gpt-4.1-mini"


def test_get_seeds_and_set_round_trips() -> None:
    session: dict = {}
    seeded = get_semantic_settings(session)
    assert isinstance(seeded, SemanticSettings)
    assert session[SEMANTIC_SETTINGS_KEY] is seeded

    updated = SemanticSettings(search_endpoint="https://x.search.windows.net")
    set_semantic_settings(session, updated)
    assert get_semantic_settings(session) is updated
