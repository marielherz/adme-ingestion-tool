#!/usr/bin/env python
"""Sample work-product-component (WPC) text fields from an ADME instance.

Read-only diagnostic for the semantic-search scoping decision. It answers
one question: *how much real narrative text lives in the WPC records in my
Volve + TNO data?* No records are modified; only Search v2 ``/query`` and
``/query_with_cursor`` reads are performed.

Auth reuses the app's Azure CLI token flow (``az login`` first). Connection
defaults target the Volve instance used elsewhere in this repo but can be
overridden with environment variables:

- ``ADME_ENDPOINT``            (default: https://marielsmrttier.energy.azure.com)
- ``ADME_DATA_PARTITION``      (default: opendes)
- ``ADME_TOKEN_SCOPE``         (default: <app-id>/.default)
- ``ADME_CLIENT_ID``           (default: ef3f6421-4b33-42b4-9184-d7c5cb2efcf2)
- ``WPC_SAMPLE_PER_KIND``      (default: 50)

Usage::

    az login
    python scripts/sample_wpc_text.py
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.connection import ADMEConnection, AuthMethod  # noqa: E402
from app.services.auth import acquire_cli_token  # noqa: E402
from app.services.search import (  # noqa: E402
    list_kinds,
    search_with_cursor,
)

# Field names that are conventionally free-text/narrative in OSDU WKS.
_KNOWN_TEXT_FIELDS = frozenset(
    {
        "Description",
        "Remarks",
        "Remark",
        "Name",
        "CommonName",
        "AcquisitionRemark",
        "VersionCreationReason",
        "StatusTechnicalDescription",
        "GeosectionDescription",
        "InterpretationRemark",
        "ProcessingRemark",
        "Comment",
        "Comments",
        "Notes",
        "Title",
        "Summary",
    }
)

# A string value counts as "narrative" when it is reasonably long OR is a
# known text field with any multi-word content.
_MIN_NARRATIVE_CHARS = 25


@dataclass
class FieldStat:
    """Accumulated fill statistics for one text field path."""

    present: int = 0
    narrative: int = 0
    total_chars: int = 0
    longest: int = 0
    example: str = ""


def _connection() -> ADMEConnection:
    client_id = os.getenv("ADME_CLIENT_ID", "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2")
    scope = os.getenv("ADME_TOKEN_SCOPE", f"{client_id}/.default")
    return ADMEConnection(
        endpoint=os.getenv("ADME_ENDPOINT", "https://marielsmrttier.energy.azure.com"),
        tenant_id=os.getenv("ADME_TENANT_ID", "72f988bf-86f1-41af-91ab-2d7cd011db47"),
        client_id=client_id,
        data_partition_id=os.getenv("ADME_DATA_PARTITION", "opendes"),
        token_scope=scope,
        auth_method=AuthMethod.USER_IMPERSONATION,
    )


def _token(connection: ADMEConnection) -> str:
    # az wants the bare resource (the scope without the "/.default" suffix).
    resource = connection.scope.removesuffix("/.default")
    return acquire_cli_token(resource=resource)


def _walk_text_fields(
    obj: object,
    prefix: str,
    stats: dict[str, FieldStat],
) -> None:
    """Recurse a record's ``data`` collecting per-path text statistics."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            _walk_text_fields(value, path, stats)
    elif isinstance(obj, list):
        # Collapse list index into ``[]`` so per-station remarks aggregate.
        for item in obj:
            _walk_text_fields(item, f"{prefix}[]", stats)
    elif isinstance(obj, str):
        text = obj.strip()
        if not text:
            return
        leaf = prefix.rsplit(".", 1)[-1].removesuffix("[]")
        is_known = leaf in _KNOWN_TEXT_FIELDS
        is_long = len(text) >= _MIN_NARRATIVE_CHARS
        multiword = " " in text
        if not (is_known or is_long):
            return
        stat = stats[prefix]
        stat.present += 1
        if is_long or (is_known and multiword):
            stat.narrative += 1
            stat.total_chars += len(text)
            if len(text) > stat.longest:
                stat.longest = len(text)
                stat.example = text[:160]


def _sample_kind(
    connection: ADMEConnection,
    token: str,
    kind: str,
    limit: int,
) -> tuple[int, dict[str, FieldStat]]:
    """Fetch up to ``limit`` records for ``kind`` and profile ``data`` text."""
    stats: dict[str, FieldStat] = defaultdict(FieldStat)
    page = search_with_cursor(
        connection,
        token,
        kind=kind,
        limit=limit,
        returned_fields=("id", "kind", "data"),
    )
    if not page.ok:
        print(f"    ! query failed (HTTP {page.http_status}): {page.error_message}")
        return 0, stats

    results = []
    if isinstance(page.raw_response, dict):
        results = page.raw_response.get("results") or []

    for record in results:
        data = record.get("data") if isinstance(record, dict) else None
        if isinstance(data, dict):
            _walk_text_fields(data, "", stats)
    return len(results), stats


def _print_kind_report(kind: str, sampled: int, stats: dict[str, FieldStat]) -> None:
    short = kind.split(":")[-2] if ":" in kind else kind
    print(f"\n=== {short}  ({sampled} records sampled) ===")
    if sampled == 0:
        print("    (no records)")
        return

    ranked = sorted(
        stats.items(),
        key=lambda kv: (kv[1].narrative, kv[1].total_chars),
        reverse=True,
    )
    narrative_paths = [(p, s) for p, s in ranked if s.narrative > 0]
    if not narrative_paths:
        print("    (no narrative text fields found)")
        return

    for path, stat in narrative_paths[:12]:
        fill = 100.0 * stat.narrative / sampled
        avg = stat.total_chars / stat.narrative if stat.narrative else 0
        print(
            f"    {path:<40} fill={fill:5.1f}%  "
            f"avg={avg:6.0f} chars  max={stat.longest}"
        )
        if stat.example:
            print(f"        e.g. {stat.example!r}")


def main() -> int:
    connection = _connection()
    print("ADME WPC text sampler")
    print("=" * 60)
    print(f"Endpoint:  {connection.endpoint}")
    print(f"Partition: {connection.data_partition_id}")
    per_kind = int(os.getenv("WPC_SAMPLE_PER_KIND", "50"))

    try:
        token = _token(connection)
    except Exception as exc:  # noqa: BLE001 - surface any auth failure plainly
        print(f"\n[ERROR] Could not acquire a token: {exc}")
        print("Run 'az login' and try again.")
        return 1

    kinds_result = list_kinds(connection, token)
    if not kinds_result.ok:
        print(f"\n[ERROR] kinds discovery failed: {kinds_result.error_message}")
        return 1

    wpc_kinds = [k for k in kinds_result.kinds if "work-product-component--" in k]
    if not wpc_kinds:
        print("\n[INFO] No work-product-component kinds found in this partition.")
        print("Kinds seen:")
        for k in kinds_result.kinds[:40]:
            print(f"    {k}")
        return 0

    print(f"\nFound {len(wpc_kinds)} WPC kind(s). Sampling up to {per_kind} each...")

    grand_total = 0
    kinds_with_text = 0
    for kind in sorted(wpc_kinds):
        sampled, stats = _sample_kind(connection, token, kind, per_kind)
        grand_total += sampled
        if any(s.narrative > 0 for s in stats.values()):
            kinds_with_text += 1
        _print_kind_report(kind, sampled, stats)

    print("\n" + "=" * 60)
    print(
        f"Summary: {grand_total} records sampled across {len(wpc_kinds)} WPC "
        f"kinds; {kinds_with_text} kind(s) contain narrative text."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
