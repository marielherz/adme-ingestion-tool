#!/usr/bin/env python3
"""Generate TNO master-data manifests from CSV via manifest_generator schemas.

Reads sample CSVs at app/data/datasets/tno/csv/{organisations,wells,wellbores}.csv,
maps columns to OSDU schema fields using manifest_generator's schema inspection
helpers, and writes flat Manifest:1.0.0 files that the bulk loader can submit.

ACL and legal arrays are intentionally empty — the bulk loader's
_inject_acl_and_legal() fills them at submit time.

Usage:
    python scripts/generate_tno_master_data.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path for bare script execution.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.models.osdu import FieldMapping  # noqa: E402
from app.services.manifest_generator import (  # noqa: E402
    _coerce_value,
    auto_map,
    extract_schema_fields,
    load_schema,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CSV_DIR = PROJECT_ROOT / "app" / "data" / "datasets" / "tno" / "csv"
OUTPUT_DIR = PROJECT_ROOT / "app" / "data" / "datasets" / "tno" / "master-data"
MANIFEST_KIND = "osdu:wks:Manifest:1.0.0"

# Entity configurations: (csv_file, osdu_kind, entity_label, output_file)
ENTITIES: list[dict[str, str]] = [
    {
        "csv": "organisations.csv",
        "kind": "osdu:wks:master-data--Organisation:1.0.0",
        "label": "Organisation",
        "output": "load_Organisation.json",
    },
    {
        "csv": "wells.csv",
        "kind": "osdu:wks:master-data--Well:1.0.0",
        "label": "Well",
        "output": "load_Well.json",
    },
    {
        "csv": "wellbores.csv",
        "kind": "osdu:wks:master-data--Wellbore:1.0.0",
        "label": "Wellbore",
        "output": "load_Wellbore.json",
    },
]

# Numbered-suffix CSV columns that form array objects.
# Pattern: {base}_{N} where N >= 1.
_NUMBERED_SUFFIX_RE = re.compile(r"^(.+)_([1-9]\d*)$")

# Map of (base_name_group) -> (array_schema_field, property_name).
# These tell us how to assemble numbered CSV columns into OSDU array items.
_ARRAY_FIELD_MAP: dict[str, tuple[str, str]] = {
    "aliasname": ("NameAliases", "AliasName"),
    "aliasnametypeid": ("NameAliases", "AliasNameTypeID"),
    "definitionorganisationid": ("NameAliases", "DefinitionOrganisationID"),
    "facilitystatetypeid": ("FacilityStates", "FacilityStateTypeID"),
    "facilitystate_effectivedatetime": ("FacilityStates", "EffectiveDateTime"),
    "facilitystate_terminationdatetime": ("FacilityStates", "TerminationDateTime"),
    "geopoliticalentityid": ("GeoContexts", "GeoPoliticalEntityID"),
}

# Simple scalar CSV→schema mappings that auto_map may miss.
# These are manually verified from Darryl's conventions doc.
_MANUAL_OVERRIDES: dict[str, dict[str, str]] = {
    "Organisation": {
        "organisationname": "OrganisationName",
        "organisationid": "OrganisationID",
    },
    "Well": {
        "facilityname": "FacilityName",
        "facilityid": "FacilityID",
        "source": "Source",
        "existencekind": "ExistenceKind",
        "currentoperatorid": "CurrentOperatorID",
        "initialoperatorid": "InitialOperatorID",
        "datasourceorganisationid": "DataSourceOrganisationID",
        "operatingenvironmentid": "OperatingEnvironmentID",
    },
    "Wellbore": {
        "facilityname": "FacilityName",
        "facilityid": "FacilityID",
        "wellid": "WellID",
        "sequencenumber": "SequenceNumber",
        "source": "Source",
        "existencekind": "ExistenceKind",
        "currentoperatorid": "CurrentOperatorID",
        "datasourceorganisationid": "DataSourceOrganisationID",
        "trajectorytypeid": "TrajectoryTypeID",
    },
}




# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [
            {k.strip(): (v.strip() if v else "") for k, v in row.items() if k is not None}
            for row in reader
        ]


# ---------------------------------------------------------------------------
# Array column grouping
# ---------------------------------------------------------------------------


def _parse_numbered_columns(
    headers: list[str],
) -> dict[str, dict[int, list[tuple[str, str, str]]]]:
    """Group numbered-suffix headers into array structures.

    Returns: {array_field: {index: [(csv_header, property_name, base)]}}
    """
    result: dict[str, dict[int, list[tuple[str, str, str]]]] = {}
    for h in headers:
        m = _NUMBERED_SUFFIX_RE.match(h.lower())
        if not m:
            continue
        base = m.group(1)
        idx = int(m.group(2))
        # Normalise base: strip underscores for lookup
        base_normalised = base.replace("_", "")
        if base_normalised not in _ARRAY_FIELD_MAP:
            # Also try with underscores (e.g. facilitystate_effectivedatetime)
            if base not in _ARRAY_FIELD_MAP:
                continue
            else:
                base_normalised = base
        array_field, prop_name = _ARRAY_FIELD_MAP[base_normalised]
        result.setdefault(array_field, {}).setdefault(idx, []).append(
            (h, prop_name, base_normalised)
        )
    return result


def _build_array_values(
    row: dict[str, str],
    array_groups: dict[str, dict[int, list[tuple[str, str, str]]]],
) -> dict[str, list[dict[str, str]]]:
    """Build array field values from a single CSV row."""
    arrays: dict[str, list[dict[str, str]]] = {}
    for array_field, indices in array_groups.items():
        items: list[dict[str, str]] = []
        for idx in sorted(indices.keys()):
            item: dict[str, str] = {}
            for csv_header, prop_name, _base in indices[idx]:
                val = row.get(csv_header, "").strip()
                if val:
                    item[prop_name] = val
            if item:  # Only include non-empty array elements
                items.append(item)
        if items:
            arrays[array_field] = items
    return arrays


# ---------------------------------------------------------------------------
# Scalar mapping: merge auto_map results with manual overrides
# ---------------------------------------------------------------------------


def _build_scalar_mapping(
    entity_label: str,
    kind: str,
    csv_headers: list[str],
) -> tuple[dict[str, str], list[str]]:
    """Return (csv_header -> schema_field, notes) for scalar fields.

    Uses auto_map for discovery, then applies manual overrides for
    fields the heuristic misses.
    """
    notes: list[str] = []

    # Identify which headers are numbered-suffix (array) columns
    array_headers: set[str] = set()
    for h in csv_headers:
        if _NUMBERED_SUFFIX_RE.match(h.lower()):
            array_headers.add(h)

    # Scalar headers only (exclude 'id' — handled as record ID, not data)
    scalar_headers = [
        h for h in csv_headers if h not in array_headers and h.lower() != "id"
    ]

    # Also exclude lat/lon from scalar mapping — handled separately
    geo_headers = {"latitude", "longitude"}
    scalar_headers_for_auto = [
        h for h in scalar_headers if h.lower() not in geo_headers
    ]

    # Run auto_map
    schema = load_schema(kind)
    schema_fields = extract_schema_fields(schema)
    mapping_result = auto_map(schema_fields, scalar_headers_for_auto)

    # Build lookup from auto_map
    header_to_field: dict[str, str] = {}
    for fm in mapping_result.mappings:
        header_to_field[fm.csv_header] = fm.schema_path

    # Apply manual overrides where auto_map didn't find the right match
    overrides = _MANUAL_OVERRIDES.get(entity_label, {})
    for csv_col_lower, schema_field in overrides.items():
        # Find the actual CSV header (case-insensitive)
        actual_header = None
        for h in scalar_headers_for_auto:
            if h.lower() == csv_col_lower:
                actual_header = h
                break
        if actual_header is None:
            continue
        if actual_header not in header_to_field:
            header_to_field[actual_header] = schema_field
            notes.append(f"  manual: {actual_header} -> {schema_field}")
        elif header_to_field[actual_header] != schema_field:
            old = header_to_field[actual_header]
            header_to_field[actual_header] = schema_field
            notes.append(
                f"  override: {actual_header}: {old} -> {schema_field}"
            )

    return header_to_field, notes


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------


def _build_record(
    row: dict[str, str],
    kind: str,
    kind_path: str,
    scalar_map: dict[str, str],
    array_groups: dict[str, dict[int, list[tuple[str, str, str]]]],
    field_type_map: dict[str, str],
) -> dict[str, Any]:
    """Build one OSDU manifest record from a CSV row."""
    row_id = row.get("id", "").strip()
    if not row_id:
        return {}

    record_id = f"osdu:{kind_path}:{row_id}"

    data: dict[str, Any] = {}

    # Scalar fields — use schema-driven type coercion from manifest_generator
    for csv_header, schema_field in scalar_map.items():
        val = row.get(csv_header, "").strip()
        if not val:
            continue
        ft = field_type_map.get(schema_field, "string")
        data[schema_field] = _coerce_value(val, ft)

    # Array fields
    arrays = _build_array_values(row, array_groups)
    data.update(arrays)

    return {
        "id": record_id,
        "kind": kind,
        "acl": {"owners": [], "viewers": []},
        "legal": {"legaltags": [], "otherRelevantDataCountries": []},
        "data": data,
    }


# ---------------------------------------------------------------------------
# Manifest envelope
# ---------------------------------------------------------------------------


def _wrap_manifest(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap records in the flat Manifest:1.0.0 envelope for master-data."""
    return {
        "kind": MANIFEST_KIND,
        "MasterData": records,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("TNO Master-Data Manifest Generation")
    print("=" * 60)

    total_records = 0
    all_notes: list[str] = []

    for entity in ENTITIES:
        csv_path = CSV_DIR / entity["csv"]
        kind = entity["kind"]
        label = entity["label"]
        output_file = OUTPUT_DIR / entity["output"]

        print(f"\n--- {label} ---")
        print(f"  CSV: {csv_path.relative_to(PROJECT_ROOT)}")

        if not csv_path.is_file():
            print(f"  ERROR: CSV not found: {csv_path}")
            continue

        rows = _read_csv(csv_path)
        headers = list(rows[0].keys()) if rows else []
        print(f"  Rows: {len(rows)}, Columns: {len(headers)}")

        # Extract kind_path (e.g. "master-data--Organisation")
        kind_parts = kind.split(":")
        kind_path = kind_parts[2] if len(kind_parts) >= 3 else kind

        # Build scalar mapping
        scalar_map, notes = _build_scalar_mapping(label, kind, headers)
        print(f"  Scalar mappings: {len(scalar_map)}")
        if notes:
            all_notes.extend([f"{label}:"] + notes)
            for n in notes:
                print(n)

        # Parse array column groups
        array_groups = _parse_numbered_columns(headers)
        array_field_names = list(array_groups.keys())
        if array_field_names:
            print(f"  Array fields: {', '.join(array_field_names)}")

        # Build field_type_map for type coercion
        schema = load_schema(kind)
        schema_fields = extract_schema_fields(schema)
        field_type_map: dict[str, str] = {
            f.path: f.field_type for f in schema_fields
        }

        # Build records
        records: list[dict[str, Any]] = []
        for row in rows:
            record = _build_record(
                row, kind, kind_path, scalar_map, array_groups, field_type_map,
            )
            if record:
                records.append(record)

        manifest = _wrap_manifest(records)

        # Write output
        output_file.write_text(
            json.dumps(manifest, indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  Records: {len(records)}")
        print(f"  Output: {output_file.relative_to(PROJECT_ROOT)}")
        total_records += len(records)

    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Total records generated: {total_records}")
    for entity in ENTITIES:
        output_file = OUTPUT_DIR / entity["output"]
        if output_file.is_file():
            body = json.loads(output_file.read_text(encoding="utf-8"))
            count = len(body.get("MasterData", []))
            print(f"  {entity['output']}: {count} records")
    if all_notes:
        print("\nManual adjustments applied:")
        for n in all_notes:
            print(f"  {n}")
    print()


if __name__ == "__main__":
    main()
