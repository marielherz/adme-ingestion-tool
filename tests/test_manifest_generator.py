"""Tests for app.services.manifest_generator."""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

import pytest

from app.models.osdu import FieldMapping, SchemaField
from app.services.manifest_generator import (
    MappingError,
    SchemaNotFoundError,
    _coerce_value,
    auto_map,
    extract_schema_fields,
    generate_manifests,
    list_schema_kinds,
    load_schema,
)

SCHEMA_DIR = (
    Path(__file__).resolve().parent.parent
    / "app"
    / "data"
    / "osdu"
    / "rc--3.0.0"
    / "schemas"
)

TNO_CSV_DIR = (
    Path(__file__).resolve().parent.parent
    / "app"
    / "data"
    / "datasets"
    / "tno"
    / "csv"
)


# ---------------------------------------------------------------------------
# list_schema_kinds
# ---------------------------------------------------------------------------


class TestListSchemaKinds:
    def test_returns_sorted_kinds(self) -> None:
        kinds = list_schema_kinds(SCHEMA_DIR)
        assert isinstance(kinds, list)
        assert len(kinds) > 0
        assert kinds == sorted(kinds)

    def test_contains_well_kind(self) -> None:
        kinds = list_schema_kinds(SCHEMA_DIR)
        assert "osdu:wks:master-data--Well:1.0.0" in kinds

    def test_contains_reference_data_kinds(self) -> None:
        kinds = list_schema_kinds(SCHEMA_DIR)
        ref_kinds = [k for k in kinds if "reference-data--" in k]
        assert len(ref_kinds) > 0

    def test_contains_organisation_kind(self) -> None:
        kinds = list_schema_kinds(SCHEMA_DIR)
        assert "osdu:wks:master-data--Organisation:1.0.0" in kinds

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        kinds = list_schema_kinds(tmp_path)
        assert kinds == []


# ---------------------------------------------------------------------------
# load_schema
# ---------------------------------------------------------------------------


class TestLoadSchema:
    def test_loads_well_schema(self) -> None:
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        assert schema["title"] == "Well"
        assert "properties" in schema

    def test_loads_organisation_schema(self) -> None:
        schema = load_schema(
            "osdu:wks:master-data--Organisation:1.0.0", SCHEMA_DIR
        )
        assert schema["title"] == "Organisation"

    def test_raises_on_unknown_kind(self) -> None:
        with pytest.raises(SchemaNotFoundError):
            load_schema("osdu:wks:master-data--Nonexistent:9.9.9", SCHEMA_DIR)

    def test_raises_on_malformed_kind(self) -> None:
        with pytest.raises(SchemaNotFoundError):
            load_schema("not-a-kind", SCHEMA_DIR)


# ---------------------------------------------------------------------------
# extract_schema_fields
# ---------------------------------------------------------------------------


class TestExtractSchemaFields:
    def test_well_schema_has_facility_name(self) -> None:
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        fields = extract_schema_fields(schema, schema_dir=SCHEMA_DIR)
        paths = [f.path for f in fields]
        assert "FacilityName" in paths

    def test_well_schema_has_source(self) -> None:
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        fields = extract_schema_fields(schema, schema_dir=SCHEMA_DIR)
        paths = [f.path for f in fields]
        assert "Source" in paths

    def test_well_schema_has_facility_id(self) -> None:
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        fields = extract_schema_fields(schema, schema_dir=SCHEMA_DIR)
        paths = [f.path for f in fields]
        assert "FacilityID" in paths

    def test_well_schema_has_name_aliases_array(self) -> None:
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        fields = extract_schema_fields(schema, schema_dir=SCHEMA_DIR)
        array_fields = [f for f in fields if f.path == "NameAliases"]
        assert len(array_fields) == 1
        assert array_fields[0].field_type == "array"

    def test_well_schema_has_name_alias_subfields(self) -> None:
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        fields = extract_schema_fields(schema, schema_dir=SCHEMA_DIR)
        paths = [f.path for f in fields]
        assert "NameAliases.AliasName" in paths
        assert "NameAliases.AliasNameTypeID" in paths

    def test_well_schema_has_current_operator(self) -> None:
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        fields = extract_schema_fields(schema, schema_dir=SCHEMA_DIR)
        paths = [f.path for f in fields]
        assert "CurrentOperatorID" in paths

    def test_no_system_fields(self) -> None:
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        fields = extract_schema_fields(schema, schema_dir=SCHEMA_DIR)
        paths = [f.path for f in fields]
        for sys_field in ("id", "kind", "version", "acl", "legal", "meta"):
            assert sys_field not in paths

    def test_organisation_has_org_name(self) -> None:
        schema = load_schema(
            "osdu:wks:master-data--Organisation:1.0.0", SCHEMA_DIR
        )
        fields = extract_schema_fields(schema, schema_dir=SCHEMA_DIR)
        paths = [f.path for f in fields]
        assert "OrganisationName" in paths

    def test_returns_list_of_schema_field(self) -> None:
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        fields = extract_schema_fields(schema, schema_dir=SCHEMA_DIR)
        assert all(isinstance(f, SchemaField) for f in fields)

    def test_empty_schema_returns_empty(self) -> None:
        fields = extract_schema_fields({})
        assert fields == []


# ---------------------------------------------------------------------------
# auto_map
# ---------------------------------------------------------------------------


class TestAutoMap:
    def _well_fields(self) -> list[SchemaField]:
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        return extract_schema_fields(schema, schema_dir=SCHEMA_DIR)

    def test_maps_tno_well_headers(self) -> None:
        fields = self._well_fields()
        headers = [
            "id", "facilityname", "facilityid", "source",
            "existencekind", "currentoperatorid",
        ]
        result = auto_map(fields, headers)
        mapped_paths = {m.schema_path for m in result.mappings}
        assert "FacilityName" in mapped_paths
        assert "FacilityID" in mapped_paths
        assert "Source" in mapped_paths
        assert "CurrentOperatorID" in mapped_paths

    def test_maps_real_tno_csv_headers(self) -> None:
        csv_path = TNO_CSV_DIR / "wells.csv"
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = [h.strip() for h in next(reader)]

        fields = self._well_fields()
        result = auto_map(fields, headers)
        mapped_paths = {m.schema_path for m in result.mappings}
        assert "FacilityName" in mapped_paths
        assert "Source" in mapped_paths

    def test_confidence_is_float(self) -> None:
        fields = self._well_fields()
        result = auto_map(fields, ["facilityname", "source"])
        assert isinstance(result.confidence, float)
        assert 0.0 <= result.confidence <= 1.0

    def test_no_matches_returns_empty_mappings(self) -> None:
        fields = self._well_fields()
        result = auto_map(fields, ["zzz_nonsense", "yyy_garbage"])
        # Should have zero or very few matches
        assert isinstance(result.mappings, list)
        assert len(result.unmatched_csv) > 0

    def test_empty_headers(self) -> None:
        fields = self._well_fields()
        result = auto_map(fields, [])
        assert result.mappings == []
        assert result.confidence == 0.0

    def test_suffix_match_catches_prefixed_header(self) -> None:
        fields = [
            SchemaField(
                path="FacilityName", field_type="string", required=False
            ),
        ]
        result = auto_map(fields, ["well_facilityname"])
        assert len(result.mappings) == 1
        assert result.mappings[0].schema_path == "FacilityName"

    def test_system_fields_excluded_from_unmatched(self) -> None:
        fields = self._well_fields()
        result = auto_map(fields, ["id", "facilityname"])
        # "id" is a system field header so should not appear in unmatched_csv
        assert "id" not in result.unmatched_csv


# ---------------------------------------------------------------------------
# generate_manifests
# ---------------------------------------------------------------------------


class TestGenerateManifests:
    def _simple_csv(self, tmp_path: Path) -> Path:
        csv_path = tmp_path / "test.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "facilityname", "source"])
            writer.writerow(["WELL-1", "Test Well 1", "TNO"])
            writer.writerow(["WELL-2", "Test Well 2", "TNO"])
        return csv_path

    def test_produces_valid_manifest(self, tmp_path: Path) -> None:
        csv_path = self._simple_csv(tmp_path)
        mapping = [
            FieldMapping(csv_header="facilityname", schema_path="FacilityName"),
            FieldMapping(csv_header="source", schema_path="Source"),
        ]
        manifests = generate_manifests(
            kind="osdu:wks:master-data--Well:1.0.0",
            csv_path=csv_path,
            mapping=mapping,
            legal_tag="opendes-public-usa-dataset-1",
            acl_owners="data.default.owners@opendes.dataservices.energy",
            acl_viewers="data.default.viewers@opendes.dataservices.energy",
            data_partition_id="opendes",
            schema_dir=SCHEMA_DIR,
        )
        assert len(manifests) == 1
        m = manifests[0]
        assert "executionContext" in m
        ctx = m["executionContext"]
        assert "manifest" in ctx
        assert ctx["manifest"]["kind"] == "osdu:wks:Manifest:1.0.0"
        records = ctx["manifest"]["MasterData"]
        assert len(records) == 2

    def test_record_has_correct_structure(self, tmp_path: Path) -> None:
        csv_path = self._simple_csv(tmp_path)
        mapping = [
            FieldMapping(csv_header="facilityname", schema_path="FacilityName"),
            FieldMapping(csv_header="source", schema_path="Source"),
        ]
        manifests = generate_manifests(
            kind="osdu:wks:master-data--Well:1.0.0",
            csv_path=csv_path,
            mapping=mapping,
            legal_tag="opendes-public-usa-dataset-1",
            acl_owners="data.default.owners@opendes.dataservices.energy",
            acl_viewers="data.default.viewers@opendes.dataservices.energy",
            data_partition_id="opendes",
            schema_dir=SCHEMA_DIR,
        )
        record = manifests[0]["executionContext"]["manifest"]["MasterData"][0]
        assert record["kind"] == "osdu:wks:master-data--Well:1.0.0"
        assert record["id"] == "opendes:master-data--Well:WELL-1"
        assert record["acl"]["owners"] == [
            "data.default.owners@opendes.dataservices.energy"
        ]
        assert record["legal"]["legaltags"] == [
            "opendes-public-usa-dataset-1"
        ]
        assert record["data"]["FacilityName"] == "Test Well 1"
        assert record["data"]["Source"] == "TNO"

    def test_reference_data_section(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "ref.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "Code"])
            writer.writerow(["Active", "Active"])
        mapping = [FieldMapping(csv_header="Code", schema_path="Code")]
        manifests = generate_manifests(
            kind="osdu:wks:reference-data--ExistenceKind:1.0.0",
            csv_path=csv_path,
            mapping=mapping,
            legal_tag="tag",
            acl_owners="owners@x",
            acl_viewers="viewers@x",
            data_partition_id="opendes",
            schema_dir=SCHEMA_DIR,
        )
        section = manifests[0]["executionContext"]["manifest"]["ReferenceData"]
        assert len(section) == 1

    def test_empty_csv_returns_empty(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "empty.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "facilityname", "source"])
            # no data rows
        mapping = [
            FieldMapping(csv_header="facilityname", schema_path="FacilityName"),
        ]
        manifests = generate_manifests(
            kind="osdu:wks:master-data--Well:1.0.0",
            csv_path=csv_path,
            mapping=mapping,
            legal_tag="tag",
            acl_owners="owners@x",
            acl_viewers="viewers@x",
            data_partition_id="opendes",
            schema_dir=SCHEMA_DIR,
        )
        assert manifests == []

    def test_omits_empty_optional_fields(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "sparse.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "facilityname", "source"])
            writer.writerow(["W1", "Well 1", ""])
        mapping = [
            FieldMapping(csv_header="facilityname", schema_path="FacilityName"),
            FieldMapping(csv_header="source", schema_path="Source"),
        ]
        manifests = generate_manifests(
            kind="osdu:wks:master-data--Well:1.0.0",
            csv_path=csv_path,
            mapping=mapping,
            legal_tag="tag",
            acl_owners="owners@x",
            acl_viewers="viewers@x",
            data_partition_id="opendes",
            schema_dir=SCHEMA_DIR,
        )
        record = manifests[0]["executionContext"]["manifest"]["MasterData"][0]
        assert "Source" not in record["data"]
        assert record["data"]["FacilityName"] == "Well 1"

    def test_raises_on_missing_kind(self, tmp_path: Path) -> None:
        csv_path = self._simple_csv(tmp_path)
        with pytest.raises(ValueError, match="non-empty kind"):
            generate_manifests(
                kind="",
                csv_path=csv_path,
                mapping=[],
                legal_tag="tag",
                acl_owners="o",
                acl_viewers="v",
                data_partition_id="opendes",
                schema_dir=SCHEMA_DIR,
            )

    def test_raises_on_unknown_kind(self, tmp_path: Path) -> None:
        csv_path = self._simple_csv(tmp_path)
        with pytest.raises(SchemaNotFoundError):
            generate_manifests(
                kind="osdu:wks:master-data--Nonexistent:9.9.9",
                csv_path=csv_path,
                mapping=[],
                legal_tag="tag",
                acl_owners="o",
                acl_viewers="v",
                data_partition_id="opendes",
                schema_dir=SCHEMA_DIR,
            )

    def test_raises_on_missing_csv(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="CSV file not found"):
            generate_manifests(
                kind="osdu:wks:master-data--Well:1.0.0",
                csv_path=tmp_path / "nope.csv",
                mapping=[],
                legal_tag="tag",
                acl_owners="o",
                acl_viewers="v",
                data_partition_id="opendes",
                schema_dir=SCHEMA_DIR,
            )

    def test_batching_over_1000_rows(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "big.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "facilityname"])
            for i in range(1050):
                writer.writerow([f"W-{i}", f"Well {i}"])
        mapping = [
            FieldMapping(csv_header="facilityname", schema_path="FacilityName"),
        ]
        manifests = generate_manifests(
            kind="osdu:wks:master-data--Well:1.0.0",
            csv_path=csv_path,
            mapping=mapping,
            legal_tag="tag",
            acl_owners="o",
            acl_viewers="v",
            data_partition_id="opendes",
            schema_dir=SCHEMA_DIR,
        )
        assert len(manifests) == 2
        first_batch = manifests[0]["executionContext"]["manifest"]["MasterData"]
        second_batch = manifests[1]["executionContext"]["manifest"]["MasterData"]
        assert len(first_batch) == 1000
        assert len(second_batch) == 50

    def test_generate_from_real_tno_well_csv(self) -> None:
        csv_path = TNO_CSV_DIR / "wells.csv"
        schema = load_schema("osdu:wks:master-data--Well:1.0.0", SCHEMA_DIR)
        fields = extract_schema_fields(schema, schema_dir=SCHEMA_DIR)

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = [h.strip() for h in next(reader)]

        result = auto_map(fields, headers)
        manifests = generate_manifests(
            kind="osdu:wks:master-data--Well:1.0.0",
            csv_path=csv_path,
            mapping=result.mappings,
            legal_tag="opendes-public-usa-dataset-1",
            acl_owners="data.default.owners@opendes.dataservices.energy",
            acl_viewers="data.default.viewers@opendes.dataservices.energy",
            data_partition_id="opendes",
            schema_dir=SCHEMA_DIR,
        )
        assert len(manifests) >= 1
        records = manifests[0]["executionContext"]["manifest"]["MasterData"]
        assert len(records) >= 1
        first = records[0]
        assert first["kind"] == "osdu:wks:master-data--Well:1.0.0"
        assert "data" in first
        assert first["data"].get("FacilityName")


# ---------------------------------------------------------------------------
# _coerce_value  (Issue #20 — type transforms)
# ---------------------------------------------------------------------------


class TestCoerceValue:
    """Unit tests for the _coerce_value helper."""

    # -- integer ---------------------------------------------------------------

    def test_integer_valid(self) -> None:
        assert _coerce_value("42", "integer") == 42
        assert isinstance(_coerce_value("42", "integer"), int)

    def test_integer_negative(self) -> None:
        assert _coerce_value("-7", "integer") == -7

    def test_integer_invalid_returns_raw(self) -> None:
        assert _coerce_value("abc", "integer") == "abc"

    def test_integer_empty_returns_empty(self) -> None:
        assert _coerce_value("", "integer") == ""

    def test_integer_float_string_returns_raw(self) -> None:
        # "3.14" is not a valid int
        assert _coerce_value("3.14", "integer") == "3.14"

    # -- number / float --------------------------------------------------------

    def test_number_valid(self) -> None:
        assert _coerce_value("3.14", "number") == 3.14
        assert isinstance(_coerce_value("3.14", "number"), float)

    def test_number_integer_string(self) -> None:
        assert _coerce_value("10", "number") == 10.0

    def test_number_negative(self) -> None:
        assert _coerce_value("-2.5", "number") == -2.5

    def test_number_invalid_returns_raw(self) -> None:
        assert _coerce_value("not-a-num", "number") == "not-a-num"

    def test_number_empty_returns_empty(self) -> None:
        assert _coerce_value("", "number") == ""

    # -- boolean ---------------------------------------------------------------

    def test_boolean_true_variants(self) -> None:
        for v in ("true", "True", "TRUE", "1", "yes", "Yes"):
            assert _coerce_value(v, "boolean") is True, f"Failed for {v!r}"

    def test_boolean_false_variants(self) -> None:
        for v in ("false", "False", "FALSE", "0", "no", "No"):
            assert _coerce_value(v, "boolean") is False, f"Failed for {v!r}"

    def test_boolean_invalid_returns_raw(self) -> None:
        assert _coerce_value("maybe", "boolean") == "maybe"

    def test_boolean_empty_returns_empty(self) -> None:
        assert _coerce_value("", "boolean") == ""

    # -- date-time -------------------------------------------------------------

    def test_datetime_valid_iso(self) -> None:
        assert _coerce_value("2024-01-15T10:30:00", "date-time") == "2024-01-15T10:30:00"

    def test_datetime_date_only(self) -> None:
        assert _coerce_value("2024-01-15", "date-time") == "2024-01-15"

    def test_datetime_with_tz(self) -> None:
        val = "2024-01-15T10:30:00+02:00"
        assert _coerce_value(val, "date-time") == val

    def test_datetime_invalid_returns_raw(self) -> None:
        assert _coerce_value("not-a-date", "date-time") == "not-a-date"

    def test_datetime_empty_returns_empty(self) -> None:
        assert _coerce_value("", "date-time") == ""

    # -- string / passthrough --------------------------------------------------

    def test_string_passthrough(self) -> None:
        assert _coerce_value("hello", "string") == "hello"

    def test_unknown_type_passthrough(self) -> None:
        assert _coerce_value("42", "foobar") == "42"

    def test_null_like_strings(self) -> None:
        # "null", "None" etc. are just strings — no special handling
        assert _coerce_value("null", "string") == "null"
        assert _coerce_value("None", "integer") == "None"


# ---------------------------------------------------------------------------
# generate_manifests — type coercion integration (Issue #20)
# ---------------------------------------------------------------------------


class TestGenerateManifestsTypeCoercion:
    """Verify that generate_manifests applies type coercion end-to-end."""

    def test_number_fields_are_floats(self, tmp_path: Path) -> None:
        """Use the Wellbore schema which has SequenceNumber (integer type)."""
        csv_path = tmp_path / "typed.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "facilityname", "sequencenumber"])
            writer.writerow(["WB-1", "Wellbore One", "3"])
        mapping = [
            FieldMapping(csv_header="facilityname", schema_path="FacilityName"),
            FieldMapping(csv_header="sequencenumber", schema_path="SequenceNumber"),
        ]
        manifests = generate_manifests(
            kind="osdu:wks:master-data--Wellbore:1.0.0",
            csv_path=csv_path,
            mapping=mapping,
            legal_tag="tag",
            acl_owners="o",
            acl_viewers="v",
            data_partition_id="opendes",
            schema_dir=SCHEMA_DIR,
        )
        record = manifests[0]["executionContext"]["manifest"]["MasterData"][0]
        seq = record["data"]["SequenceNumber"]
        assert isinstance(seq, int)
        assert seq == 3

    def test_string_fields_stay_strings(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "str.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "facilityname"])
            writer.writerow(["W-1", "Well One"])
        mapping = [
            FieldMapping(csv_header="facilityname", schema_path="FacilityName"),
        ]
        manifests = generate_manifests(
            kind="osdu:wks:master-data--Well:1.0.0",
            csv_path=csv_path,
            mapping=mapping,
            legal_tag="tag",
            acl_owners="o",
            acl_viewers="v",
            data_partition_id="opendes",
            schema_dir=SCHEMA_DIR,
        )
        record = manifests[0]["executionContext"]["manifest"]["MasterData"][0]
        assert isinstance(record["data"]["FacilityName"], str)

    def test_invalid_number_kept_as_string(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "bad_num.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "sequencenumber"])
            writer.writerow(["WB-1", "not-a-number"])
        mapping = [
            FieldMapping(csv_header="sequencenumber", schema_path="SequenceNumber"),
        ]
        manifests = generate_manifests(
            kind="osdu:wks:master-data--Wellbore:1.0.0",
            csv_path=csv_path,
            mapping=mapping,
            legal_tag="tag",
            acl_owners="o",
            acl_viewers="v",
            data_partition_id="opendes",
            schema_dir=SCHEMA_DIR,
        )
        record = manifests[0]["executionContext"]["manifest"]["MasterData"][0]
        seq = record["data"]["SequenceNumber"]
        assert seq == "not-a-number"
