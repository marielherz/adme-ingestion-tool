"""Convert Open Test Data provided CSV templates into JSON manifests."""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

__all__ = [
    "convert_provided_csv_tree",
    "csv_to_manifest_body",
]

_ARRAY_TOKEN = re.compile(r"_(\d+)$")
_LIST_TIER_SECTIONS = {"reference-data": "ReferenceData", "master-data": "MasterData"}


def convert_provided_csv_tree(root: Path, *, out_dir_name: str = "generated-json") -> Path:
    """Convert CSV templates under ``root/Volve/provided`` into JSON manifests.

    Returns the generated root. The layout mirrors the source provided tree so
    existing downloaded-dataset discovery can load the generated JSON folder.
    """
    provided = _find_provided(root)
    if provided is None:
        raise ValueError(f"Could not find a Volve/provided folder under {root}")

    generated = root / out_dir_name / "provided"
    for csv_path in sorted(provided.rglob("*.csv")):
        rel = csv_path.relative_to(provided)
        tier = rel.parts[0] if rel.parts else ""
        if tier not in {"reference-data", "master-data", "work-products"}:
            continue
        manifest = csv_to_manifest_body(csv_path, tier=tier)
        if manifest is None:
            continue
        out_path = generated / rel.with_suffix(".json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return generated.parent


def csv_to_manifest_body(csv_path: Path, *, tier: str) -> dict[str, Any] | None:
    """Convert one provided CSV file to a bare Manifest body.

    Bare means the returned JSON starts with ``{"kind": "osdu:wks:Manifest..."}``,
    matching the TNO files consumed by the downloaded-dataset loaders.
    """
    rows = _read_rows(csv_path)
    if not rows:
        return None
    if tier in _LIST_TIER_SECTIONS:
        records = [_record_from_flat_row(row) for row in rows]
        records = [record for record in records if record is not None]
        records = _dedupe_records(records)
        if not records:
            return None
        return {
            "kind": "osdu:wks:Manifest:1.0.0",
            _LIST_TIER_SECTIONS[tier]: records,
        }
    if tier == "work-products":
        return _work_product_manifest(rows)
    return None


def _find_provided(root: Path) -> Path | None:
    for candidate in (
        root / "Volve" / "provided",
        root / "Volve",
        root / "provided",
        root,
    ):
        if (candidate / "reference-data").is_dir() or (
            candidate / "work-products"
        ).is_dir():
            return candidate
    return None


def _read_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [
            {str(k or "").strip(): str(v or "").strip() for k, v in row.items()}
            for row in reader
        ]


def _record_from_flat_row(row: dict[str, str]) -> dict[str, Any] | None:
    record_id = row.get("id", "").strip()
    kind = row.get("kind", "").strip()
    if not record_id or not kind:
        return None
    record: dict[str, Any] = {"id": _record_id(record_id, kind), "kind": kind}
    version = row.get("version", "").strip()
    if version:
        record["version"] = _coerce(version)
    record["acl"] = _owners_viewers(row, prefix="acl_")
    record["legal"] = _legal(row, prefix="legal_")
    data = _nested_from_prefixed(row, "data_")
    if data:
        record["data"] = data
    ancestry = _nested_from_prefixed(row, "ancestry_")
    if ancestry:
        record["ancestry"] = ancestry
    return record


def _record_id(value: str, kind: str) -> str:
    """Return a full OSDU record id for a CSV row id value."""
    if ":" in value:
        return value
    parts = kind.split(":")
    kind_path = parts[2] if len(parts) >= 3 else kind
    return f"osdu:{kind_path}:{value}"


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for record in records:
        record_id = record.get("id")
        key = record_id if isinstance(record_id, str) else ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        output.append(record)
    return output


def _work_product_manifest(rows: list[dict[str, str]]) -> dict[str, Any] | None:
    wp_rows: dict[str, dict[str, str]] = {}
    wpc_rows: dict[str, dict[str, str]] = {}
    dataset_rows: dict[str, dict[str, str]] = {}

    for row in rows:
        wp_id = row.get("wp_id", "").strip()
        wpc_id = row.get("wpc_id_1", "").strip() or row.get("wpc_id", "").strip()
        dataset_id = row.get("dataset_id_1", "").strip() or row.get(
            "dataset_id", ""
        ).strip()
        if wp_id and wp_id not in wp_rows:
            wp_rows[wp_id] = row
        if wpc_id and wpc_id not in wpc_rows:
            wpc_rows[wpc_id] = row
        if dataset_id and dataset_id not in dataset_rows:
            dataset_rows[dataset_id] = row

    work_products = [
        _record_from_prefixed_row(row, prefix="wp_", id_key="id", kind_key="kind")
        for row in wp_rows.values()
    ]
    components = [
        _record_from_prefixed_row(row, prefix="wpc_", id_key="id_1", kind_key="kind_1")
        for row in wpc_rows.values()
    ]
    datasets = [
        _record_from_prefixed_row(
            row, prefix="dataset_", id_key="id_1", kind_key="kind_1"
        )
        for row in dataset_rows.values()
    ]
    work_products = [record for record in work_products if record is not None]
    components = [record for record in components if record is not None]
    datasets = [record for record in datasets if record is not None]
    if not (work_products or components or datasets):
        return None

    data: dict[str, Any] = {}
    if work_products:
        data["WorkProduct"] = work_products[0]
    if components:
        data["WorkProductComponents"] = components
    if datasets:
        data["Datasets"] = datasets
    return {"kind": "osdu:wks:Manifest:1.0.0", "Data": data}


def _record_from_prefixed_row(
    row: dict[str, str],
    *,
    prefix: str,
    id_key: str,
    kind_key: str,
) -> dict[str, Any] | None:
    record_id = row.get(f"{prefix}{id_key}", "").strip()
    kind = row.get(f"{prefix}{kind_key}", "").strip()
    if not record_id or not kind:
        return None
    record: dict[str, Any] = {"id": record_id, "kind": kind}
    normalized = _strip_instance_suffixes(row, prefix=prefix)
    record["acl"] = _owners_viewers(normalized, prefix=f"{prefix}acl_")
    record["legal"] = _legal(normalized, prefix=f"{prefix}legal_")
    data = _nested_from_prefixed(normalized, f"{prefix}data_")
    if data:
        record["data"] = data
    return record


def _strip_instance_suffixes(row: dict[str, str], *, prefix: str) -> dict[str, str]:
    """Drop the final template instance suffix for one WP/WPC/Dataset record.

    Provided work-product CSV templates suffix nearly every WPC/Dataset field
    with a record instance number (for example ``wpc_data_Name_1``). That
    trailing ``_1`` is not part of the OSDU field shape, while earlier numeric
    tokens still represent real arrays (for example ``Datasets_1``).
    """
    output: dict[str, str] = {}
    for key, value in row.items():
        if not key.startswith(prefix):
            output[key] = value
            continue
        if _has_instance_suffix(key):
            output[key.rsplit("_", 1)[0]] = value
        else:
            output[key] = value
    return output


def _has_instance_suffix(key: str) -> bool:
    base, _, suffix = key.rpartition("_")
    if not suffix.isdigit() or not base:
        return False
    # Two numeric suffixes means the last one is the record instance and the
    # previous one is an actual list index, e.g. Datasets_1_1 -> Datasets_1.
    prev = base.rsplit("_", 1)[-1]
    if prev.isdigit():
        return True
    return key.startswith(("wpc_", "dataset_"))


def _owners_viewers(row: dict[str, str], *, prefix: str) -> dict[str, list[str]]:
    owners = _collect_prefixed_list(row, f"{prefix}owners")
    viewers = _collect_prefixed_list(row, f"{prefix}viewers")
    return {"owners": owners, "viewers": viewers}


def _legal(row: dict[str, str], *, prefix: str) -> dict[str, Any]:
    legal: dict[str, Any] = {}
    tags = _collect_prefixed_list(row, f"{prefix}legaltags")
    countries = _collect_prefixed_list(row, f"{prefix}otherRelevantDataCountries")
    status = row.get(f"{prefix}status", "").strip()
    legal["legaltags"] = tags
    legal["otherRelevantDataCountries"] = countries
    if status:
        legal["status"] = status
    return legal


def _collect_prefixed_list(row: dict[str, str], base: str) -> list[str]:
    values: list[tuple[int, str]] = []
    direct = row.get(base, "").strip()
    if direct:
        values.append((0, direct))
    for key, value in row.items():
        if not value:
            continue
        suffix = key.removeprefix(base)
        if suffix == key or not suffix.startswith("_"):
            continue
        if suffix[1:].isdigit():
            values.append((int(suffix[1:]), value.strip()))
    return [value for _, value in sorted(values) if value]


def _nested_from_prefixed(row: dict[str, str], prefix: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for key, value in row.items():
        if not value or not key.startswith(prefix):
            continue
        path = key.removeprefix(prefix)
        _set_path(root, path, _coerce(value))
    return _prune_empty(root)


def _set_path(root: dict[str, Any], flat_path: str, value: Any) -> None:
    tokens = _tokens(flat_path)
    current: Any = root
    for index, token in enumerate(tokens[:-1]):
        following = tokens[index + 1]
        next_container = [] if isinstance(following, int) else {}
        current = _ensure_child(current, token, next_container)
    _assign(current, tokens[-1], value)


def _tokens(flat_path: str) -> list[str | int]:
    tokens: list[str | int] = []
    for raw_part in flat_path.split("_"):
        if not raw_part:
            continue
        part = raw_part.replace("$$", "")
        if part.isdigit():
            tokens.append(int(part) - 1)
            continue
        match = _ARRAY_TOKEN.search(part)
        if match:
            name = part[: match.start()]
            if name:
                tokens.append(name)
            tokens.append(int(match.group(1)) - 1)
            continue
        tokens.append(part)
    return tokens or [flat_path]


def _ensure_child(container: Any, token: str | int, default: Any) -> Any:
    if isinstance(token, int):
        if not isinstance(container, list):
            return default
        while len(container) <= token:
            container.append({})
        if not container[token]:
            container[token] = default
        return container[token]
    if not isinstance(container, dict):
        return default
    if token not in container or container[token] in (None, ""):
        container[token] = default
    return container[token]


def _assign(container: Any, token: str | int, value: Any) -> None:
    if isinstance(token, int):
        if not isinstance(container, list):
            return
        while len(container) <= token:
            container.append(None)
        container[token] = value
        return
    if isinstance(container, dict):
        existing = container.get(token)
        if existing is None:
            container[token] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            container[token] = [existing, value]


def _coerce(value: str) -> Any:
    stripped = value.strip()
    if not stripped:
        return ""
    upper = stripped.upper()
    if upper == "TRUE":
        return True
    if upper == "FALSE":
        return False
    if upper in {"NULL", "NONE"}:
        return None
    return stripped


def _prune_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: item
            for key, item in (
                (key, _prune_empty(item)) for key, item in value.items()
            )
            if item not in ({}, [], None, "")
        }
    if isinstance(value, list):
        return [
            item
            for item in (_prune_empty(item) for item in value)
            if item not in ({}, [], None, "")
        ]
    return value
