"""ADME Search v2 + Storage v2 read-only client for the Search page.

Sibling to :mod:`app.services.verification` (which owns the
post-ingest ``search_records_by_kind`` probe and stays untouched). This
module powers the Operate › Search page: paged ``/query`` calls, a
best-effort kinds-discovery helper, and a single-record fetch off
Storage.

Pattern follows :mod:`app.services.legal_tags` and
:mod:`app.services.entitlements`: stdlib + ``requests`` only, a single
per-call timeout, *no internal retries* — the Streamlit page owns the
re-run UX. Every public function returns a frozen result dataclass from
:mod:`app.models.osdu` so the UI never has to handle holes.

References:
- ``.squad/decisions/inbox/darryl-search-api.md`` for API facts.
- ``.squad/decisions/inbox/satya-search-page-contract.md`` for the
  locked v1 page contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from time import perf_counter
from typing import Any
from urllib.parse import quote

import requests  # type: ignore[import-untyped]

from app.models.connection import ADMEConnection
from app.models.osdu import (
    AggregationBucket,
    CursorSearchResult,
    KindAggregationResult,
    RecordDetailResult,
    RecordSummary,
    SearchAggregationResult,
    SearchPageResult,
)

SEARCH_QUERY_PATH = "/api/search/v2/query"
SEARCH_CURSOR_QUERY_PATH = "/api/search/v2/query_with_cursor"
STORAGE_RECORD_PATH_TEMPLATE = "/api/storage/v2/records/{record_id}"

SEARCH_TIMEOUT_SECONDS = 15
EXPORT_TIMEOUT_SECONDS = 30
DEFAULT_SEARCH_LIMIT = 100
MAX_OFFSET_PLUS_LIMIT = 10_000
WILDCARD_KIND = "*:*:*:*"

# Default field projection for list views. ``createTime`` is excluded
# from ``returnedFields`` because it lives at the top level of a hit and
# Search returns top-level fields by default; we keep this list focused
# on payload-shape fields the page renders.
_DEFAULT_RETURNED_FIELDS: tuple[str, ...] = (
    "id",
    "kind",
    "createTime",
    "modifyTime",
    "version",
)

_DEFAULT_SORT: dict[str, list[str]] = {
    "field": ["createTime"],
    "order": ["DESC"],
}

_CORRELATION_HEADER_NAMES: tuple[str, ...] = (
    "correlation-id",
    "x-correlation-id",
    "request-id",
    "x-request-id",
)

_ERROR_BODY_TEXT_LIMIT = 500


# --- public API ---------------------------------------------------------


def search_records(
    connection: ADMEConnection,
    token: str,
    *,
    kind: str,
    query: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
) -> SearchPageResult:
    """``POST /api/search/v2/query`` for one page of summaries.

    Returns a :class:`SearchPageResult` with summaries projected from
    each hit. Empty ``query`` is omitted from the body (Lucene rejects
    empty strings on some indexers — Darryl §5.1). Raises
    :class:`ValueError` on empty ``kind`` or invalid pagination.
    """
    if not kind or not kind.strip():
        raise ValueError("A non-empty kind is required for search_records.")
    if limit < 1:
        raise ValueError("limit must be >= 1.")
    if offset < 0:
        raise ValueError("offset must be >= 0.")
    if offset + limit > MAX_OFFSET_PLUS_LIMIT:
        raise ValueError(
            "offset + limit must be <= "
            f"{MAX_OFFSET_PLUS_LIMIT} (OSDU Search ceiling)."
        )

    body: dict[str, Any] = {
        "kind": kind,
        "limit": limit,
        "offset": offset,
        "sort": dict(_DEFAULT_SORT),
        "returnedFields": list(_DEFAULT_RETURNED_FIELDS),
    }
    trimmed_query = query.strip() if isinstance(query, str) else ""
    if trimmed_query:
        body["query"] = trimmed_query

    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_search(
            connection=connection,
            token=token,
            method="POST",
            path=SEARCH_QUERY_PATH,
            json_body=body,
        )
    )

    if http_status is None:
        return SearchPageResult(
            kind=kind,
            query=trimmed_query or None,
            offset=offset,
            limit=limit,
            ok=False,
            http_status=None,
            latency_ms=latency_ms,
            correlation_id=None,
            error_message=error_message,
            raw_response=None,
        )

    if 200 <= http_status < 300:
        payload = parsed_body if isinstance(parsed_body, dict) else {}
        records = _parse_record_summaries(payload.get("results"))
        total_count = _coerce_int(payload.get("totalCount"))
        if total_count is not None:
            has_more = (offset + len(records)) < total_count
        else:
            has_more = len(records) >= limit
        return SearchPageResult(
            kind=kind,
            query=trimmed_query or None,
            offset=offset,
            limit=limit,
            records=records,
            total_count=total_count,
            has_more=has_more,
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            error_message=None,
            raw_response=parsed_body,
        )

    return SearchPageResult(
        kind=kind,
        query=trimmed_query or None,
        offset=offset,
        limit=limit,
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        correlation_id=correlation_id,
        error_message=error_message,
        raw_response=parsed_body,
    )


def build_multi_kind_query(
    kinds: list[str],
    base_query: str | None = None,
) -> tuple[str, str]:
    """Build a kind + query for multi-kind search.

    OSDU doesn't support kind arrays in /query, so multi-kind search uses
    a wildcard kind with a Lucene kind filter in the query string.

    Returns (effective_kind, effective_query).
    """
    base = base_query.strip() if isinstance(base_query, str) else ""

    if len(kinds) == 1:
        return (kinds[0], base)

    if len(kinds) > 1:
        kind_clauses = " OR ".join(f'kind:"{k}"' for k in kinds)
        kind_filter = f"({kind_clauses})"
        if base:
            return (WILDCARD_KIND, f"{kind_filter} AND {base}")
        return (WILDCARD_KIND, kind_filter)

    # Empty list
    return (WILDCARD_KIND, base)


def search_with_aggregation(
    connection: ADMEConnection,
    token: str,
    *,
    kind: str = WILDCARD_KIND,
    query: str | None = None,
    aggregate_by: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
) -> SearchAggregationResult:
    """``POST /api/search/v2/query`` with optional ``aggregateBy``.

    When ``aggregate_by`` is provided (e.g. ``"kind"``), the response
    includes an ``aggregations`` array with ``{"key": …, "count": N}``
    entries. When omitted, behaves like :func:`search_records` but
    returns the richer :class:`SearchAggregationResult` type.
    """
    if not kind or not kind.strip():
        raise ValueError(
            "A non-empty kind is required for search_with_aggregation."
        )
    if limit < 1:
        raise ValueError("limit must be >= 1.")
    if offset < 0:
        raise ValueError("offset must be >= 0.")
    if offset + limit > MAX_OFFSET_PLUS_LIMIT:
        raise ValueError(
            "offset + limit must be <= "
            f"{MAX_OFFSET_PLUS_LIMIT} (OSDU Search ceiling)."
        )

    body: dict[str, Any] = {
        "kind": kind,
        "limit": limit,
        "offset": offset,
        "sort": dict(_DEFAULT_SORT),
        "returnedFields": list(_DEFAULT_RETURNED_FIELDS),
    }
    trimmed_query = query.strip() if isinstance(query, str) else ""
    if trimmed_query:
        body["query"] = trimmed_query
    if aggregate_by:
        body["aggregateBy"] = aggregate_by

    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_search(
            connection=connection,
            token=token,
            method="POST",
            path=SEARCH_QUERY_PATH,
            json_body=body,
        )
    )

    if http_status is None:
        return SearchAggregationResult(
            kind=kind,
            query=trimmed_query or None,
            offset=offset,
            limit=limit,
            ok=False,
            http_status=None,
            latency_ms=latency_ms,
            correlation_id=None,
            error_message=error_message,
            raw_response=None,
        )

    if 200 <= http_status < 300:
        payload = parsed_body if isinstance(parsed_body, dict) else {}
        records = _parse_record_summaries(payload.get("results"))
        total_count = _coerce_int(payload.get("totalCount"))
        if total_count is not None:
            has_more = (offset + len(records)) < total_count
        else:
            has_more = len(records) >= limit
        aggregations = _parse_aggregation_buckets(
            payload.get("aggregations")
        )
        return SearchAggregationResult(
            kind=kind,
            query=trimmed_query or None,
            offset=offset,
            limit=limit,
            records=records,
            total_count=total_count,
            has_more=has_more,
            aggregations=aggregations,
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            error_message=None,
            raw_response=parsed_body,
        )

    return SearchAggregationResult(
        kind=kind,
        query=trimmed_query or None,
        offset=offset,
        limit=limit,
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        correlation_id=correlation_id,
        error_message=error_message,
        raw_response=parsed_body,
    )


def search_with_cursor(
    connection: ADMEConnection,
    token: str,
    *,
    kind: str,
    query: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    cursor: str | None = None,
    returned_fields: tuple[str, ...] | None = None,
) -> CursorSearchResult:
    """``POST /api/search/v2/query_with_cursor`` for one page.

    On the first call, omit ``cursor`` (or pass ``None``); on subsequent
    calls, pass the ``cursor`` returned in the previous result. The
    response ``has_more`` flag indicates whether more pages remain.

    Uses ``returned_fields`` if provided, otherwise
    ``_DEFAULT_RETURNED_FIELDS``.
    """
    if not kind or not kind.strip():
        raise ValueError(
            "A non-empty kind is required for search_with_cursor."
        )
    if limit < 1:
        raise ValueError("limit must be >= 1.")

    fields = list(returned_fields) if returned_fields else list(
        _DEFAULT_RETURNED_FIELDS
    )

    body: dict[str, Any] = {
        "kind": kind,
        "limit": limit,
        "sort": dict(_DEFAULT_SORT),
        "returnedFields": fields,
    }
    trimmed_query = query.strip() if isinstance(query, str) else ""
    if trimmed_query:
        body["query"] = trimmed_query
    if cursor:
        body["cursor"] = cursor

    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_search(
            connection=connection,
            token=token,
            method="POST",
            path=SEARCH_CURSOR_QUERY_PATH,
            json_body=body,
            timeout=EXPORT_TIMEOUT_SECONDS,
        )
    )

    if http_status is None:
        return CursorSearchResult(
            kind=kind,
            query=trimmed_query or None,
            cursor=None,
            limit=limit,
            ok=False,
            http_status=None,
            latency_ms=latency_ms,
            correlation_id=None,
            error_message=error_message,
            raw_response=None,
        )

    if 200 <= http_status < 300:
        payload = parsed_body if isinstance(parsed_body, dict) else {}
        records = _parse_record_summaries(payload.get("results"))
        total_count = _coerce_int(payload.get("totalCount"))
        next_cursor = payload.get("cursor") or None
        return CursorSearchResult(
            kind=kind,
            query=trimmed_query or None,
            cursor=next_cursor,
            limit=limit,
            records=records,
            total_count=total_count,
            has_more=bool(next_cursor),
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            error_message=None,
            raw_response=parsed_body,
        )

    return CursorSearchResult(
        kind=kind,
        query=trimmed_query or None,
        cursor=None,
        limit=limit,
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        correlation_id=correlation_id,
        error_message=error_message,
        raw_response=parsed_body,
    )


def export_all_records(
    connection: ADMEConnection,
    token: str,
    *,
    kind: str,
    query: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    returned_fields: tuple[str, ...] | None = None,
) -> Iterator[CursorSearchResult]:
    """Yield :class:`CursorSearchResult` pages until exhaustion or error.

    Iterates ``search_with_cursor`` calls, feeding each response's
    ``cursor`` into the next request. Stops when ``has_more`` is
    ``False`` or an error page is encountered (yielded so the caller
    sees the failure).
    """
    next_cursor: str | None = None
    while True:
        page = search_with_cursor(
            connection,
            token,
            kind=kind,
            query=query,
            limit=limit,
            cursor=next_cursor,
            returned_fields=returned_fields,
        )
        yield page
        if not page.ok or not page.has_more:
            break
        next_cursor = page.cursor


def list_kinds(
    connection: ADMEConnection,
    token: str,
) -> KindAggregationResult:
    """Best-effort kinds discovery for the page's dropdown.

    Tries Search aggregation first (one round-trip, includes counts).
    If aggregation is rejected, returns an empty list, or fails at the
    transport layer, falls back to a single ``/query`` page and harvests
    unique ``kind`` values from the hits. ``from_aggregation`` records
    which path actually produced the result.
    """
    agg_body: dict[str, Any] = {
        "kind": WILDCARD_KIND,
        "aggregateBy": "kind",
        "limit": 0,
    }

    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_search(
            connection=connection,
            token=token,
            method="POST",
            path=SEARCH_QUERY_PATH,
            json_body=agg_body,
        )
    )

    if http_status is not None and 200 <= http_status < 300:
        payload = parsed_body if isinstance(parsed_body, dict) else {}
        kinds = _parse_aggregation_kinds(payload.get("aggregations"))
        if kinds:
            return KindAggregationResult(
                kinds=kinds,
                from_aggregation=True,
                ok=True,
                http_status=http_status,
                latency_ms=latency_ms,
                correlation_id=correlation_id,
                error_message=None,
                raw_response=parsed_body,
            )
        # Aggregation succeeded but came back empty — fall through to
        # the page-sample path so the dropdown can still be populated
        # from real records if any exist.

    # Fallback: sample the first page and extract distinct kinds.
    sample_body: dict[str, Any] = {
        "kind": WILDCARD_KIND,
        "limit": DEFAULT_SEARCH_LIMIT,
        "offset": 0,
        "returnedFields": ["kind"],
    }
    (
        sample_parsed,
        sample_status,
        sample_correlation,
        sample_latency,
        sample_error,
    ) = _call_search(
        connection=connection,
        token=token,
        method="POST",
        path=SEARCH_QUERY_PATH,
        json_body=sample_body,
    )

    total_latency = round(latency_ms + sample_latency, 2)
    effective_correlation = sample_correlation or correlation_id

    if sample_status is None:
        # Transport failure on the fallback — surface the aggregation
        # error if we had one, otherwise the sample error.
        return KindAggregationResult(
            kinds=[],
            from_aggregation=False,
            ok=False,
            http_status=http_status,
            latency_ms=total_latency,
            correlation_id=effective_correlation,
            error_message=error_message or sample_error,
            raw_response=sample_parsed,
        )

    if 200 <= sample_status < 300:
        payload = sample_parsed if isinstance(sample_parsed, dict) else {}
        kinds = _harvest_kinds_from_results(payload.get("results"))
        return KindAggregationResult(
            kinds=kinds,
            from_aggregation=False,
            ok=True,
            http_status=sample_status,
            latency_ms=total_latency,
            correlation_id=effective_correlation,
            error_message=None,
            raw_response=sample_parsed,
        )

    return KindAggregationResult(
        kinds=[],
        from_aggregation=False,
        ok=False,
        http_status=sample_status,
        latency_ms=total_latency,
        correlation_id=effective_correlation,
        error_message=sample_error or error_message,
        raw_response=sample_parsed,
    )


def get_record(
    connection: ADMEConnection,
    token: str,
    record_id: str,
) -> RecordDetailResult:
    """``GET /api/storage/v2/records/{record_id}`` — full record fetch.

    ``record_id`` is URL-encoded with ``quote(id, safe=":")`` per
    Darryl's §3 guidance (preserve colons inside the path segment,
    encode everything else).
    """
    if not record_id or not record_id.strip():
        raise ValueError("A non-empty record_id is required for get_record.")

    quoted = quote(record_id, safe=":")
    path = STORAGE_RECORD_PATH_TEMPLATE.format(record_id=quoted)

    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_search(
            connection=connection,
            token=token,
            method="GET",
            path=path,
            json_body=None,
        )
    )

    if http_status is None:
        return RecordDetailResult(
            record_id=record_id,
            record=None,
            ok=False,
            http_status=None,
            latency_ms=latency_ms,
            correlation_id=None,
            error_message=error_message,
            raw_response=None,
        )

    if 200 <= http_status < 300:
        record = parsed_body if isinstance(parsed_body, dict) else None
        return RecordDetailResult(
            record_id=record_id,
            record=record,
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            error_message=None,
            raw_response=parsed_body,
        )

    if http_status == 404:
        friendly = f"Record '{record_id}' not found or not visible."
        return RecordDetailResult(
            record_id=record_id,
            record=None,
            ok=False,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            error_message=friendly,
            raw_response=parsed_body,
        )

    return RecordDetailResult(
        record_id=record_id,
        record=None,
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        correlation_id=correlation_id,
        error_message=error_message,
        raw_response=parsed_body,
    )


# --- internal helpers ---------------------------------------------------


def _parse_record_summaries(raw: object) -> list[RecordSummary]:
    if not isinstance(raw, list):
        return []
    summaries: list[RecordSummary] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        id_raw = entry.get("id")
        kind_raw = entry.get("kind")
        if not isinstance(id_raw, str) or not isinstance(kind_raw, str):
            continue
        create_time_raw = entry.get("createTime")
        create_time = (
            create_time_raw if isinstance(create_time_raw, str) else None
        )
        version = _coerce_int(entry.get("version"))
        source_raw = entry.get("source")
        if isinstance(source_raw, dict):
            source = dict(source_raw)
        else:
            # Many Search responses inline the projected fields at the
            # hit level rather than under a dedicated ``source`` block.
            # Keep a shallow copy of the hit (minus the fields we
            # already promoted) so the page has something to preview.
            source = {
                k: v
                for k, v in entry.items()
                if k not in {"id", "kind", "createTime", "version"}
            }
        summaries.append(
            RecordSummary(
                id=id_raw,
                kind=kind_raw,
                create_time=create_time,
                version=version,
                source=source,
            )
        )
    return summaries


def _parse_aggregation_kinds(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if isinstance(key, str) and key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _parse_aggregation_buckets(raw: object) -> list[AggregationBucket]:
    """Parse aggregation buckets from the Search v2 response."""
    if not isinstance(raw, list):
        return []
    buckets: list[AggregationBucket] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        count = entry.get("count")
        if not isinstance(key, str) or not key:
            continue
        coerced_count = _coerce_int(count)
        if coerced_count is None:
            continue
        buckets.append(AggregationBucket(key=key, count=coerced_count))
    return buckets


def _harvest_kinds_from_results(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if isinstance(kind, str) and kind and kind not in seen:
            seen.add(kind)
            out.append(kind)
    return sorted(out)


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _call_search(
    connection: ADMEConnection,
    token: str,
    *,
    method: str,
    path: str,
    json_body: dict | None,
    timeout: int = SEARCH_TIMEOUT_SECONDS,
) -> tuple[dict | str | None, int | None, str | None, float, str | None]:
    if not connection.is_valid():
        raise ValueError(
            "ADME connection is incomplete. Endpoint, tenant ID, "
            "client ID, and data partition ID are required. Service "
            "principal auth also requires a client secret."
        )
    if not token.strip():
        raise ValueError(
            "A non-empty bearer token is required for search calls."
        )

    url = f"{connection.endpoint.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "data-partition-id": connection.data_partition_id,
        "Accept": "application/json",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    started_at = perf_counter()

    try:
        if method == "GET":
            response = requests.get(
                url=url,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
        elif method == "POST":
            response = requests.post(
                url=url,
                headers=headers,
                json=json_body,
                timeout=timeout,
                allow_redirects=False,
            )
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
    except requests.Timeout:
        return (
            None,
            None,
            None,
            _elapsed_ms(started_at),
            f"Request timed out after {timeout}s",
        )
    except requests.RequestException as exc:
        return (
            None,
            None,
            None,
            _elapsed_ms(started_at),
            f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # pragma: no cover - defensive boundary
        return (
            None,
            None,
            None,
            _elapsed_ms(started_at),
            f"{type(exc).__name__}: {exc}",
        )

    latency_ms = _elapsed_ms(started_at)
    correlation_id = _extract_correlation_id(
        response.headers, _CORRELATION_HEADER_NAMES
    )
    parsed_body = _try_parse_json(response)

    if 200 <= response.status_code < 300:
        return (
            parsed_body,
            response.status_code,
            correlation_id,
            latency_ms,
            None,
        )

    if parsed_body is not None and isinstance(parsed_body, dict):
        error_message = _error_message_from_json(
            parsed_body, response.status_code
        )
        return (
            parsed_body,
            response.status_code,
            correlation_id,
            latency_ms,
            error_message,
        )

    text = getattr(response, "text", "") or ""
    body: dict | str | None = text if text else None
    error_message = (
        _truncate(text) if text else f"HTTP {response.status_code}"
    )
    return (
        body,
        response.status_code,
        correlation_id,
        latency_ms,
        error_message,
    )


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 2)


def _extract_correlation_id(
    headers: object,
    candidate_names: Iterable[str],
) -> str | None:
    if headers is None:
        return None
    lowercase_lookup: dict[str, str] = {}
    try:
        items = headers.items()  # type: ignore[attr-defined]
    except AttributeError:
        return None
    for key, value in items:
        if isinstance(key, str) and isinstance(value, str):
            lowercase_lookup[key.lower()] = value
    for name in candidate_names:
        value = lowercase_lookup.get(name.lower())
        if value:
            return value
    return None


def _try_parse_json(response: requests.Response) -> dict | None:
    response_json = getattr(response, "json", None)
    if not callable(response_json):
        return None
    try:
        payload = response_json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _error_message_from_json(payload: dict, status_code: int) -> str:
    for key in ("message", "detail", "error", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value)

    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return _truncate(str(errors[0]))
    if isinstance(errors, dict) and errors:
        first_value = next(iter(errors.values()))
        return _truncate(str(first_value))

    return f"HTTP {status_code}"


def _truncate(value: str, limit: int = _ERROR_BODY_TEXT_LIMIT) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3]}..."
