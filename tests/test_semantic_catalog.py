"""Tests for the generalized semantic catalog summarizers."""

from __future__ import annotations

from app.services.semantic_catalog import (
    CATALOG_INDEX_NAME,
    build_catalog_index,
    document_to_semantic,
    summarize_record,
    summarize_records,
    well_to_semantic,
    wellbore_to_semantic,
)


def test_document_summary_anchors_wellbore() -> None:
    record = {
        "id": "opendes:work-product-component--Document:AUR-01-final-report",
        "kind": "osdu:wks:work-product-component--Document:1.0.0",
        "data": {
            "Name": "AUR-01 Final Well Report",
            "Description": "Flags a shallow-gas hazard and total losses.",
            "WellboreID": "opendes:master-data--Wellbore:AUR-01-01:",
        },
    }
    doc = document_to_semantic(record)
    assert doc is not None
    assert doc.source == "document"
    assert doc.anchor_wellbore_id == "opendes:master-data--Wellbore:AUR-01-01"
    assert doc.anchor_well_id is None
    assert "shallow-gas" in doc.content
    assert "AUR-01 Final Well Report" in doc.content


def test_well_summary_anchors_itself() -> None:
    record = {
        "id": "opendes:master-data--Well:AUR-01",
        "kind": "osdu:wks:master-data--Well:1.0.0",
        "data": {
            "FacilityName": "Aurelia Discovery 1",
            "NameAliases": [{"AliasName": "Aurelia Discovery 1"}],
        },
    }
    doc = well_to_semantic(record)
    assert doc is not None
    assert doc.source == "well"
    assert doc.anchor_well_id == "opendes:master-data--Well:AUR-01"
    assert doc.title == "Aurelia Discovery 1"


def test_wellbore_summary_anchors_well_and_self() -> None:
    record = {
        "id": "opendes:master-data--Wellbore:AUR-01-ST1",
        "kind": "osdu:wks:master-data--Wellbore:1.0.0",
        "data": {
            "FacilityName": "Aurelia Discovery 1 sidetrack 1",
            "Remark": "Geosteered back into Hugin after losses.",
            "WellID": "opendes:master-data--Well:AUR-01:",
        },
    }
    doc = wellbore_to_semantic(record)
    assert doc is not None
    assert doc.source == "wellbore"
    assert doc.anchor_well_id == "opendes:master-data--Well:AUR-01"
    assert doc.anchor_wellbore_id == "opendes:master-data--Wellbore:AUR-01-ST1"
    assert "Hugin" in doc.content


def test_key_is_search_safe() -> None:
    record = {
        "id": "opendes:master-data--Well:AUR-01",
        "kind": "osdu:wks:master-data--Well:1.0.0",
        "data": {"FacilityName": "Aurelia Discovery 1"},
    }
    doc = well_to_semantic(record)
    assert doc is not None
    # AI Search keys forbid ':' '.' '/'; the key must contain none.
    assert ":" not in doc.key
    assert "/" not in doc.key
    assert doc.key.startswith("well__")


def test_summarize_record_dispatch_and_skip() -> None:
    well = {
        "id": "opendes:master-data--Well:W1",
        "kind": "osdu:wks:master-data--Well:1.0.0",
        "data": {"FacilityName": "W1"},
    }
    unknown = {
        "id": "opendes:master-data--Field:F1",
        "kind": "osdu:wks:master-data--Field:1.0.0",
        "data": {},
    }
    assert summarize_record(well) is not None
    assert summarize_record(unknown) is None
    result = summarize_records([well, unknown])
    assert len(result.documents) == 1
    assert result.skipped == 1


def test_index_schema_has_expected_fields() -> None:
    index = build_catalog_index()
    names = {f.name for f in index.fields}
    assert index.name == CATALOG_INDEX_NAME
    assert {"id", "source", "title", "content", "contentVector"} <= names
    assert {"anchorWellId", "anchorWellboreId", "recordId", "kind"} <= names


def test_to_index_doc_round_trips_fields() -> None:
    record = {
        "id": "opendes:work-product-component--Document:D1",
        "kind": "osdu:wks:work-product-component--Document:1.0.0",
        "data": {
            "Name": "Report",
            "Description": "text",
            "WellboreID": "opendes:master-data--Wellbore:WB1:",
        },
    }
    doc = document_to_semantic(record)
    assert doc is not None
    index_doc = doc.to_index_doc([0.1, 0.2, 0.3])
    assert index_doc["source"] == "document"
    assert index_doc["recordId"] == record["id"]
    assert index_doc["anchorWellboreId"] == "opendes:master-data--Wellbore:WB1"
    assert index_doc["contentVector"] == [0.1, 0.2, 0.3]
