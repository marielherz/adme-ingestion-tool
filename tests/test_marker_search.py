"""Tests for the marker search service configuration and result mapping."""

from __future__ import annotations

import pytest

from app.services.marker_search import (
    MarkerHit,
    MarkerSearchConfig,
    MarkerSearchError,
    _to_hit,
    search_markers,
)


def _config(**overrides: str) -> MarkerSearchConfig:
    base = {
        "search_endpoint": "https://search.example.net",
        "search_key": "sk",
        "foundry_endpoint": "https://foundry.example.com",
        "foundry_key": "fk",
    }
    base.update(overrides)
    return MarkerSearchConfig(**base)  # type: ignore[arg-type]


def test_config_is_complete_requires_all_fields() -> None:
    assert _config().is_complete()
    assert not _config(search_key="").is_complete()
    assert not _config(foundry_endpoint="").is_complete()


def test_search_rejects_incomplete_config() -> None:
    with pytest.raises(MarkerSearchError, match="incomplete"):
        search_markers(_config(search_key=""), "sandstone")


def test_search_returns_empty_for_blank_query() -> None:
    assert search_markers(_config(), "   ") == []


def test_to_hit_prefers_reranker_score() -> None:
    hit = _to_hit(
        {
            "markerName": "Slochteren Formation",
            "geologicalAges": ["Permian"],
            "occurrenceCount": 6,
            "wellboreCount": 4,
            "depthMin": 100.0,
            "depthMax": 200.0,
            "@search.score": 0.5,
            "@search.reranker_score": 1.9,
        }
    )
    assert isinstance(hit, MarkerHit)
    assert hit.marker_name == "Slochteren Formation"
    assert hit.rank_score == 1.9


def test_to_hit_falls_back_to_search_score() -> None:
    hit = _to_hit({"markerName": "X", "@search.score": 0.7})
    assert hit.rank_score == 0.7
    assert hit.geological_ages == []
