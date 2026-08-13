"""Manifest Generator — schema-driven CSV-to-manifest production.

Scans vendored OSDU schemas, extracts mappable fields, performs heuristic
column-to-field matching, and produces workflow-ready manifest dicts. Pure
Python, no network, no Streamlit.

Contract: ``.squad/decisions/inbox/satya-manifest-generator-contract.md``.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.models.osdu import FieldMapping, MappingResult, SchemaField

logger = logging.getLogger(__name__)

__all__ = [
    "auto_map",
    "extract_schema_fields",
    "generate_manifests",
    "list_schema_kinds",
    "load_schema",
    "MappingError",
    "SchemaNotFoundError",
]

SCHEMA_ROOT: Path = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "osdu"
    / "rc--3.0.0"
    / "schemas"
).resolve()

MANIFEST_WRAPPER_KIND = "osdu:wks:Manifest:1.0.0"

_SYSTEM_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "kind",
        "version",
        "acl",
        "legal",
        "meta",
        "tags",
        "ancestry",
        "createTime",
        "createUser",
        "modifyTime",
        "modifyUser",
    }
)

# Schema sub-directories that contain entity schemas (not abstract/type).
_ENTITY_DIRS: tuple[str, ...] = (
    "master-data",
    "reference-data",
    "work-product-component",
    "dataset",
    "work-product",
)

_MANIFEST_BATCH_SIZE = 1000

# Section name inside the Manifest envelope, keyed by the kind group.
_KIND_GROUP_TO_SECTION: dict[str, str] = {
    "master-data": "MasterData",
    "reference-data": "ReferenceData",
    "work-product-component": "Data",
    "dataset": "Data",
    "work-product": "Data",
}


# -- Exceptions --------------------------------------------------------------


class SchemaNotFoundError(Exception):
    """Raised when a requested kind has no vendored schema."""


class MappingError(Exception):
    """Raised when required schema fields have no mapping."""


# -- Public API ---------------------------------------------------------------


def list_schema_kinds(schema_dir: Path | None = None) -> list[str]:
    """Return sorted OSDU kind strings for all vendored schemas."""
    root = _resolve_schema_dir(schema_dir)
    kinds: list[str] = []
    for subdir_name in _ENTITY_DIRS:
        subdir = root / subdir_name
        if not subdir.is_dir():
            continue
        for fpath in sorted(subdir.iterdir()):
            if not fpath.suffix == ".json":
                continue
            try:
                with open(fpath, encoding="utf-8") as f:
                    schema = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            kind = schema.get("x-osdu-schema-source")
            if kind:
                kinds.append(kind)
    return sorted(set(kinds))


def load_schema(kind: str, schema_dir: Path | None = None) -> dict:
    """Load the raw JSON schema for an OSDU kind.

    Raises ``SchemaNotFoundError`` if the kind is not vendored.
    """
    root = _resolve_schema_dir(schema_dir)
    # kind looks like "osdu:wks:master-data--Well:1.0.0"
    parts = kind.split(":")
    if len(parts) < 4:
        raise SchemaNotFoundError(f"Malformed kind string: {kind}")

    type_part = parts[2]  # e.g. "master-data--Well"
    version = parts[3]  # e.g. "1.0.0"

    if "--" in type_part:
        group, entity = type_part.split("--", 1)
    else:
        raise SchemaNotFoundError(f"No group separator in kind: {kind}")

    filename = f"{entity}.{version}.json"
    path = root / group / filename
    if not path.is_file():
        raise SchemaNotFoundError(
            f"Schema file not found: {path.relative_to(root)}"
        )

    with open(path, encoding="utf-8") as f:
        return json.load(f)


def extract_schema_fields(
    schema: dict,
    *,
    schema_dir: Path | None = None,
) -> list[SchemaField]:
    """Walk the schema ``data`` block and return mappable leaf fields."""
    root = _resolve_schema_dir(schema_dir)
    data_spec = schema.get("properties", {}).get("data")
    if data_spec is None:
        return []

    required_top: set[str] = set(schema.get("required", []))
    fields: list[SchemaField] = []
    _walk_data_spec(data_spec, root, schema, "", fields, required_top)
    return fields


def auto_map(
    schema_fields: list[SchemaField],
    csv_headers: list[str],
) -> MappingResult:
    """Heuristic v1: fuzzy name-matching of CSV headers to schema fields.

    Never raises — unmapped columns are the operator's problem.
    """
    if not csv_headers:
        return MappingResult(
            mappings=[],
            unmatched_csv=[],
            unmatched_required=[
                f.path for f in schema_fields if f.required
            ],
            confidence=0.0,
        )

    # Build normalised lookup: normalised_schema_leaf → SchemaField
    norm_fields: dict[str, SchemaField] = {}
    for sf in schema_fields:
        norm_fields[_normalise(sf.path.rsplit(".", 1)[-1])] = sf

    # Full dotted path normalised → SchemaField (for full-path matches)
    norm_full: dict[str, SchemaField] = {}
    for sf in schema_fields:
        norm_full[_normalise(sf.path)] = sf

    used_fields: set[str] = set()  # SchemaField.path values already matched
    mappings: list[FieldMapping] = []
    remaining_headers: list[str] = list(csv_headers)

    # Strip numbered-suffix CSV headers for base-name matching
    header_bases: dict[str, str] = {}  # normalised base → original header
    for h in csv_headers:
        header_bases[_normalise(h)] = h

    # --- Pass 1: exact match (normalised) ---
    still_remaining: list[str] = []
    for h in remaining_headers:
        nh = _normalise(h)
        matched = _exact_match(nh, norm_fields, norm_full, used_fields)
        if matched:
            mappings.append(FieldMapping(csv_header=h, schema_path=matched.path))
            used_fields.add(matched.path)
        else:
            still_remaining.append(h)
    remaining_headers = still_remaining

    # --- Pass 2: suffix match ---
    still_remaining = []
    for h in remaining_headers:
        nh = _normalise(h)
        matched = _suffix_match(nh, norm_fields, used_fields)
        if matched:
            mappings.append(FieldMapping(csv_header=h, schema_path=matched.path))
            used_fields.add(matched.path)
        else:
            still_remaining.append(h)
    remaining_headers = still_remaining

    # --- Pass 3: Jaccard token overlap ---
    still_remaining = []
    for h in remaining_headers:
        nh = _normalise(h)
        matched = _jaccard_match(nh, schema_fields, used_fields)
        if matched:
            mappings.append(FieldMapping(csv_header=h, schema_path=matched.path))
            used_fields.add(matched.path)
        else:
            still_remaining.append(h)
    remaining_headers = still_remaining

    # Filter out system-like CSV headers (e.g. "id") from unmatched_csv
    unmatched_csv = [
        h
        for h in remaining_headers
        if _normalise(h) not in {_normalise(s) for s in _SYSTEM_FIELDS}
    ]

    required_fields = [f for f in schema_fields if f.required]
    unmatched_required = [
        f.path for f in required_fields if f.path not in used_fields
    ]
    total_required = len(required_fields)
    matched_required = total_required - len(unmatched_required)
    confidence = matched_required / total_required if total_required else 1.0

    return MappingResult(
        mappings=mappings,
        unmatched_csv=unmatched_csv,
        unmatched_required=unmatched_required,
        confidence=confidence,
    )


def generate_manifests(
    *,
    kind: str,
    csv_path: Path,
    mapping: list[FieldMapping],
    legal_tag: str,
    acl_owners: str,
    acl_viewers: str,
    data_partition_id: str,
    schema_dir: Path | None = None,
) -> list[dict]:
    """Produce workflow-ready manifest dicts (one per batch of CSV rows).

    Raises:
        ValueError: if required inputs are missing/empty.
        SchemaNotFoundError: if the kind is not vendored.
        MappingError: if a required schema field has no mapping.
    """
    _require("kind", kind)
    _require("legal_tag", legal_tag)
    _require("acl_owners", acl_owners)
    _require("acl_viewers", acl_viewers)
    _require("data_partition_id", data_partition_id)

    if not csv_path.is_file():
        raise ValueError(f"CSV file not found: {csv_path}")

    schema = load_schema(kind, schema_dir)
    schema_fields = extract_schema_fields(schema, schema_dir=schema_dir)

    # Validate that all required schema fields have mappings
    mapped_paths = {m.schema_path for m in mapping}
    required_unmapped = [
        f.path
        for f in schema_fields
        if f.required and f.path not in mapped_paths
    ]
    if required_unmapped:
        raise MappingError(
            f"Required schema fields have no mapping: {required_unmapped}"
        )

    # Determine manifest section from kind
    section = _section_for_kind(kind)

    # Build header→field mapping lookup
    header_to_path: dict[str, str] = {m.csv_header: m.schema_path for m in mapping}

    # Build path→field_type lookup for type coercion
    path_to_type: dict[str, str] = {f.path: f.field_type for f in schema_fields}

    # Derive the kind_path for record IDs (e.g. "master-data--Well")
    kind_parts = kind.split(":")
    kind_path = kind_parts[2] if len(kind_parts) >= 3 else kind

    # Read CSV
    rows = _read_csv(csv_path)
    if not rows:
        return []

    records: list[dict[str, Any]] = []
    for row in rows:
        data_block = _build_data_block(row, header_to_path, path_to_type)
        if not data_block:
            continue

        # Row identifier: prefer "id" column, fall back to row index
        row_id = row.get("id", "").strip()
        if not row_id:
            row_id = str(len(records))

        record_id = f"{data_partition_id}:{kind_path}:{row_id}"

        record: dict[str, Any] = {
            "id": record_id,
            "kind": kind,
            "acl": {
                "owners": [acl_owners],
                "viewers": [acl_viewers],
            },
            "legal": {
                "legaltags": [legal_tag],
                "otherRelevantDataCountries": ["US"],
                "status": "compliant",
            },
            "data": data_block,
        }
        records.append(record)

    # Batch into manifests
    manifests: list[dict] = []
    for i in range(0, max(len(records), 1), _MANIFEST_BATCH_SIZE):
        batch = records[i : i + _MANIFEST_BATCH_SIZE]
        if not batch:
            continue
        manifest = _wrap_manifest(batch, section, data_partition_id)
        manifests.append(manifest)

    return manifests


# -- Internal helpers ---------------------------------------------------------


def _resolve_schema_dir(schema_dir: Path | None) -> Path:
    return schema_dir.resolve() if schema_dir else SCHEMA_ROOT


def _require(field_name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(
            f"A non-empty {field_name} is required for generate_manifests."
        )


def _normalise(name: str) -> str:
    """Lowercase and strip separators for matching."""
    return re.sub(r"[_\-\s]", "", name).lower()


def _tokenise(name: str) -> set[str]:
    """Split on separators and case boundaries, lowercase all."""
    # Split camelCase / PascalCase
    parts = re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
    return {t.lower() for t in re.split(r"[_\-\s]+", parts) if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _exact_match(
    normalised_header: str,
    norm_fields: dict[str, SchemaField],
    norm_full: dict[str, SchemaField],
    used: set[str],
) -> SchemaField | None:
    # Try leaf match first
    for nk, sf in norm_fields.items():
        if sf.path in used:
            continue
        if normalised_header == nk:
            return sf
    # Try full dotted-path match
    for nk, sf in norm_full.items():
        if sf.path in used:
            continue
        if normalised_header == nk:
            return sf
    return None


def _suffix_match(
    normalised_header: str,
    norm_fields: dict[str, SchemaField],
    used: set[str],
) -> SchemaField | None:
    best: SchemaField | None = None
    best_len = 0
    for nk, sf in norm_fields.items():
        if sf.path in used:
            continue
        if normalised_header.endswith(nk) and len(nk) > best_len:
            best = sf
            best_len = len(nk)
    return best


def _jaccard_match(
    normalised_header: str,
    schema_fields: list[SchemaField],
    used: set[str],
    threshold: float = 0.5,
) -> SchemaField | None:
    header_tokens = _tokenise(normalised_header)
    best: SchemaField | None = None
    best_score = threshold
    for sf in schema_fields:
        if sf.path in used:
            continue
        field_tokens = _tokenise(sf.path.rsplit(".", 1)[-1])
        score = _jaccard(header_tokens, field_tokens)
        if score >= best_score:
            best = sf
            best_score = score
    return best


# -- Schema walking ----------------------------------------------------------


def _walk_data_spec(
    spec: dict,
    schema_root: Path,
    parent_schema: dict,
    prefix: str,
    out: list[SchemaField],
    required_names: set[str],
) -> None:
    """Recursively walk a data spec, resolving allOf/$ref."""
    if "$ref" in spec:
        resolved = _resolve_ref(spec["$ref"], schema_root, parent_schema)
        if resolved:
            _walk_data_spec(
                resolved, schema_root, parent_schema, prefix, out, required_names
            )
        return

    if "allOf" in spec:
        for item in spec["allOf"]:
            _walk_data_spec(
                item, schema_root, parent_schema, prefix, out, required_names
            )
        return

    if "oneOf" in spec:
        # Take first variant for field discovery (e.g. GeoContext)
        for item in spec["oneOf"]:
            _walk_data_spec(
                item, schema_root, parent_schema, prefix, out, required_names
            )
        return

    props = spec.get("properties", {})

    for name, prop_spec in props.items():
        if name in _SYSTEM_FIELDS:
            continue
        if name == "ExtensionProperties":
            continue

        dotted = name if not prefix else f"{prefix}.{name}"
        field_type = prop_spec.get("type", "string")

        if field_type == "array":
            # Array of objects: walk items for sub-fields
            items = prop_spec.get("items", {})
            if items.get("type") == "object" or "allOf" in items or "$ref" in items:
                out.append(
                    SchemaField(
                        path=dotted,
                        field_type="array",
                        required=name in required_names,
                        description=prop_spec.get("description", ""),
                    )
                )
                _walk_data_spec(
                    items,
                    schema_root,
                    parent_schema,
                    dotted,
                    out,
                    set(),
                )
            else:
                # Array of primitives
                out.append(
                    SchemaField(
                        path=dotted,
                        field_type="array",
                        required=name in required_names,
                        description=prop_spec.get("description", ""),
                    )
                )
        elif field_type == "object" and "properties" in prop_spec:
            # Nested object: walk children
            _walk_data_spec(
                prop_spec,
                schema_root,
                parent_schema,
                dotted,
                out,
                set(),  # nested object required is structural, not for CSV
            )
        elif "$ref" in prop_spec:
            # Property is a $ref to an abstract type
            resolved = _resolve_ref(
                prop_spec["$ref"], schema_root, parent_schema
            )
            if resolved:
                resolved_type = resolved.get("type", "object")
                if resolved_type == "object" and "properties" in resolved:
                    _walk_data_spec(
                        resolved,
                        schema_root,
                        parent_schema,
                        dotted,
                        out,
                        set(),  # nested $ref required is structural, not for CSV
                    )
                else:
                    out.append(
                        SchemaField(
                            path=dotted,
                            field_type=resolved_type,
                            required=name in required_names,
                            description=prop_spec.get(
                                "description",
                                resolved.get("description", ""),
                            ),
                        )
                    )
        else:
            # Leaf scalar
            out.append(
                SchemaField(
                    path=dotted,
                    field_type=field_type,
                    required=name in required_names,
                    description=prop_spec.get("description", ""),
                )
            )


def _resolve_ref(
    ref: str,
    schema_root: Path,
    parent_schema: dict,
) -> dict | None:
    """Resolve a $ref path relative to the schema directory."""
    if ref.startswith("#/"):
        # Internal JSON pointer (rare in OSDU schemas, but handle it)
        parts = ref[2:].split("/")
        node: Any = parent_schema
        for p in parts:
            if isinstance(node, dict):
                node = node.get(p)
            else:
                return None
        return node if isinstance(node, dict) else None

    # Relative file reference like "../abstract/AbstractFacility.1.0.0.json"
    # or same-dir like "AbstractAnyCrsFeatureCollection.1.0.0.json"
    # We resolve relative to any schema dir containing the entity dirs.
    # Heuristic: $ref paths are relative to the file that declares them.
    # Since we always load top-level from schema_root/<group>/<entity>.json,
    # a ref like "../abstract/Foo.json" resolves from schema_root/<group>/.
    # We try both master-data and the root itself.
    candidates = [
        schema_root / ref.lstrip("./"),
    ]
    # Also try from each entity dir (for "../abstract/..." refs)
    for subdir_name in _ENTITY_DIRS:
        candidates.append((schema_root / subdir_name / ref).resolve())
    # Direct abstract path
    if ref.startswith("Abstract") or not ref.startswith("."):
        candidates.append(schema_root / "abstract" / ref)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            try:
                with open(resolved, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

    logger.debug("Could not resolve $ref: %s", ref)
    return None


# -- Manifest construction ---------------------------------------------------


def _read_csv(csv_path: Path) -> list[dict[str, str]]:
    """Read a CSV into a list of row dicts. Headers are stripped."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [
            {k.strip(): v.strip() if v else "" for k, v in row.items()}
            for row in reader
        ]


_BOOLEAN_TRUE: frozenset[str] = frozenset({"true", "1", "yes"})
_BOOLEAN_FALSE: frozenset[str] = frozenset({"false", "0", "no"})


def _coerce_value(raw: str, field_type: str) -> Any:
    """Coerce a CSV string value to the Python type implied by *field_type*.

    Supported field_type values:
      - ``integer`` → int
      - ``number``  → float
      - ``boolean`` → bool (accepts true/false/1/0/yes/no, case-insensitive)
      - ``date-time`` → validated ISO 8601 string (pass-through if valid)
      - ``string`` / anything else → no-op, return raw

    On coercion failure the raw string is returned and a warning is logged.
    """
    if not raw:
        return raw

    ft = field_type.lower()

    if ft == "integer":
        try:
            return int(raw)
        except (ValueError, TypeError):
            logger.warning("Cannot coerce %r to integer, keeping raw string", raw)
            return raw

    if ft == "number":
        try:
            return float(raw)
        except (ValueError, TypeError):
            logger.warning("Cannot coerce %r to number/float, keeping raw string", raw)
            return raw

    if ft == "boolean":
        lower = raw.strip().lower()
        if lower in _BOOLEAN_TRUE:
            return True
        if lower in _BOOLEAN_FALSE:
            return False
        logger.warning("Cannot coerce %r to boolean, keeping raw string", raw)
        return raw

    if ft == "date-time":
        try:
            datetime.fromisoformat(raw)
            return raw
        except (ValueError, TypeError):
            logger.warning(
                "Cannot validate %r as ISO 8601 date-time, keeping raw string", raw
            )
            return raw

    # string or unrecognised type → pass-through
    return raw


def _build_data_block(
    row: dict[str, str],
    header_to_path: dict[str, str],
    path_to_type: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a nested data dict from a CSV row and mapping."""
    data: dict[str, Any] = {}
    for csv_header, schema_path in header_to_path.items():
        value = row.get(csv_header, "").strip()
        if not value:
            continue
        if path_to_type:
            field_type = path_to_type.get(schema_path, "string")
            typed_value = _coerce_value(value, field_type)
        else:
            typed_value = value
        _set_nested(data, schema_path, typed_value)
    return data


def _set_nested(data: dict[str, Any], dotted_path: str, value: str) -> None:
    """Set a value at a dotted path in a nested dict."""
    parts = dotted_path.split(".")
    current = data
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def _section_for_kind(kind: str) -> str:
    """Determine the manifest section from the kind string."""
    parts = kind.split(":")
    if len(parts) >= 3:
        type_part = parts[2]
        if "--" in type_part:
            group = type_part.split("--", 1)[0]
            section = _KIND_GROUP_TO_SECTION.get(group)
            if section:
                return section
    return "MasterData"


def _wrap_manifest(
    records: list[dict[str, Any]],
    section: str,
    data_partition_id: str,
) -> dict:
    """Wrap records in the Manifest:1.0.0 envelope."""
    manifest_body: dict[str, Any] = {
        "kind": MANIFEST_WRAPPER_KIND,
        "ReferenceData": [],
        "MasterData": [],
        "Data": [],
    }
    manifest_body[section] = records

    return {
        "executionContext": {
            "Payload": {
                "AppKey": "adme-ingestion-tool",
                "data-partition-id": data_partition_id,
            },
            "manifest": manifest_body,
        },
    }
