"""Tests for the grounded discovery answer synthesizer."""

from __future__ import annotations

from app.services.answer import (
    build_citations,
    compose_answer,
    synthesize_answer,
)
from app.services.discovery import DiscoveryResult
from app.services.marker_search import MarkerHit
from app.services.semantic_catalog import CatalogHit


def _catalog_hit(title, record_id, source="document", content="", well=None):
    return CatalogHit(
        record_id=record_id,
        source=source,
        kind="osdu:wks:work-product-component--Document:1.0.0",
        title=title,
        content=content,
        anchor_well_id=well,
        anchor_wellbore_id=None,
        score=0.9,
        reranker_score=2.0,
    )


def _marker(name):
    return MarkerHit(
        marker_name=name,
        geological_ages=["Middle Jurassic"],
        occurrence_count=5,
        wellbore_count=3,
        depth_min=1000.0,
        depth_max=2000.0,
        score=0.8,
        reranker_score=1.5,
    )


def _result() -> DiscoveryResult:
    return DiscoveryResult(
        query="which wells took total losses on a fault?",
        concepts=[_marker("Hugin Formation")],
        catalog_hits=[
            _catalog_hit(
                "AUR-01 Final Well Report",
                "opendes:work-product-component--Document:AUR-01-final-report",
                content="Total circulation losses on a fault forced a sidetrack.",
                well=None,
            )
        ],
        anchor_well_ids=[
            "opendes:master-data--Well:AUR-01",
            "opendes:master-data--Well:AUR-02",
        ],
        concept_wells={"Hugin Formation": ["opendes:master-data--Well:AUR-01"]},
    )


def test_compose_answer_points_to_records() -> None:
    text = compose_answer(_result())
    assert "AUR-01 Final Well Report" in text
    assert "AUR-01-final-report" in text  # cites the record id tail
    assert "AUR-01" in text and "AUR-02" in text  # anchor wells
    assert "Hugin Formation" in text  # concept
    assert "total circulation losses" in text.lower()  # grounded in content


def test_build_citations_dedupes_and_orders() -> None:
    citations = build_citations(_result())
    ids = [c.record_id for c in citations]
    # report first, then the two anchor wells, no dupes
    assert ids[0].endswith("AUR-01-final-report")
    assert "opendes:master-data--Well:AUR-01" in ids
    assert "opendes:master-data--Well:AUR-02" in ids
    assert len(ids) == len(set(ids))


def test_synthesize_answer_composed_without_chat_deployment() -> None:
    answer = synthesize_answer(_result())
    assert answer.generated_by == "composed"
    assert answer.citations
    assert "AUR-01 Final Well Report" in answer.text


def test_synthesize_answer_empty_result() -> None:
    answer = synthesize_answer(DiscoveryResult(query="nothing"))
    assert answer.generated_by == "composed"
    assert "Nothing matched" in answer.text


def test_synthesize_answer_falls_back_when_llm_errors(monkeypatch) -> None:
    import app.services.answer as answer_mod

    def _boom(*a, **k):
        raise RuntimeError("chat deployment not found")

    monkeypatch.setattr(answer_mod, "llm_answer", _boom)
    answer = synthesize_answer(
        _result(),
        chat_deployment="gpt-4o-mini",
        foundry_endpoint="https://x.services.ai.azure.com",
        foundry_key="key",
    )
    # Falls back to the deterministic composed answer, never raises.
    assert answer.generated_by == "composed"
    assert "AUR-01" in answer.text
