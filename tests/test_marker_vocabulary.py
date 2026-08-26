"""Tests for marker vocabulary aggregation."""

from __future__ import annotations

from app.services.marker_vocabulary import (
    MarkerVocabularyEntry,
    aggregate_markers,
    normalize_name,
)


def _record(wellbore: str, markers: list[dict]) -> dict:
    return {"data": {"WellboreID": wellbore, "Markers": markers}}


def test_normalize_name_collapses_case_and_space() -> None:
    assert normalize_name("  Bentheim   Sandstone  Member ") == (
        "bentheim sandstone member"
    )


def test_aggregate_dedupes_and_counts_wellbores() -> None:
    records = [
        _record(
            "opendes:master-data--Wellbore:1:",
            [
                {"MarkerName": "Bentheim Sandstone Member", "GeologicalAge": "Cretaceous", "MarkerMeasuredDepth": 1200.0},
                {"MarkerName": "FAULT"},
            ],
        ),
        _record(
            "opendes:master-data--Wellbore:2:",
            [
                {"MarkerName": "bentheim sandstone member", "MarkerMeasuredDepth": 1500.0},
                {"MarkerName": "Ommelanden Formation", "GeologicalAge": "Cretaceous"},
            ],
        ),
    ]

    entries = aggregate_markers(records)
    by_norm = {e.normalized_name: e for e in entries}

    assert "fault" not in by_norm  # noise excluded
    bentheim = by_norm["bentheim sandstone member"]
    assert bentheim.occurrence_count == 2
    assert bentheim.wellbore_count == 2
    assert bentheim.depth_min == 1200.0
    assert bentheim.depth_max == 1500.0
    assert bentheim.geological_ages == ["Cretaceous"]
    assert isinstance(bentheim, MarkerVocabularyEntry)


def test_aggregate_sorted_by_occurrence_desc() -> None:
    records = [
        _record("w:1", [{"MarkerName": "A"}, {"MarkerName": "A"}, {"MarkerName": "B"}]),
    ]
    entries = aggregate_markers(records)
    assert [e.marker_name for e in entries] == ["A", "B"]


def test_search_text_includes_age() -> None:
    entry = MarkerVocabularyEntry(
        id="marker-x",
        marker_name="Slochteren Formation",
        normalized_name="slochteren formation",
        geological_ages=["Permian"],
    )
    assert entry.to_search_text() == "Slochteren Formation | Geological age: Permian"


def test_empty_and_missing_names_ignored() -> None:
    records = [_record("w:1", [{"MarkerName": "  "}, {"NotAName": "x"}, {}])]
    assert aggregate_markers(records) == []
