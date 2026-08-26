"""Tests for the OSDU relationship-edge catalog extractor."""

from __future__ import annotations

from app.services.osdu_edge_catalog import (
    EdgeDef,
    class_name_from_schema,
    extract_relationship_edges,
)


def test_extracts_direct_relationship_edge() -> None:
    schema = {
        "title": "Wellbore",
        "properties": {
            "data": {
                "allOf": [
                    {
                        "properties": {
                            "WellID": {
                                "type": "string",
                                "x-osdu-relationship": [{"EntityType": "Well"}],
                            }
                        }
                    }
                ]
            }
        },
    }
    edges = extract_relationship_edges(schema)
    assert EdgeDef("WellID", ("Well",), False) in edges


def test_flags_array_relationship_as_one_to_many() -> None:
    schema = {
        "properties": {
            "GeologicalFormationIDs": {
                "type": "array",
                "items": {"x-osdu-relationship": [{"EntityType": "GeologicalFormation"}]},
            }
        }
    }
    edges = {e.property_name: e for e in extract_relationship_edges(schema)}
    assert edges["GeologicalFormationIDs"].is_array is True
    assert edges["GeologicalFormationIDs"].target_entity_types == ("GeologicalFormation",)


def test_ignores_non_id_and_unannotated_fields() -> None:
    schema = {
        "properties": {
            "FacilityName": {"type": "string"},
            "SpatialLocationID": {"type": "string"},  # ID but no relationship
        }
    }
    assert extract_relationship_edges(schema) == []


def test_multiple_entity_types_captured() -> None:
    schema = {
        "properties": {
            "InterpretationID": {
                "x-osdu-relationship": [
                    {"EntityType": "HorizonInterpretation"},
                    {"EntityType": "FaultInterpretation"},
                ]
            }
        }
    }
    edges = extract_relationship_edges(schema)
    assert edges[0].target_entity_types == (
        "HorizonInterpretation",
        "FaultInterpretation",
    )


def test_class_name_prefers_title() -> None:
    assert class_name_from_schema({"title": "Wellbore"}, "k") == "Wellbore"
    assert class_name_from_schema({}, "master-data--Well") == "master-data--Well"
