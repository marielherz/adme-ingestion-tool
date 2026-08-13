"""Tests for ``app.services.search``: search_records, list_kinds, get_record."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import pytest
import requests  # type: ignore[import-untyped]

from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import (
    AggregationBucket,
    CursorSearchResult,
    KindAggregationResult,
    RecordDetailResult,
    RecordSummary,
    SearchAggregationResult,
    SearchPageResult,
)
from app.services import search as search_module
from app.services.search import (
    DEFAULT_SEARCH_LIMIT,
    EXPORT_TIMEOUT_SECONDS,
    MAX_OFFSET_PLUS_LIMIT,
    SEARCH_CURSOR_QUERY_PATH,
    SEARCH_QUERY_PATH,
    SEARCH_TIMEOUT_SECONDS,
    STORAGE_RECORD_PATH_TEMPLATE,
    WILDCARD_KIND,
    build_multi_kind_query,
    export_all_records,
    get_record,
    list_kinds,
    search_records,
    search_with_aggregation,
    search_with_cursor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    json_payload: object | None = None
    body: str = ""
    raise_on_json: bool = False

    @property
    def text(self) -> str:
        return self.body

    def json(self) -> object:
        if self.raise_on_json or self.json_payload is None:
            raise ValueError("No JSON payload")
        return self.json_payload


def _connection(
    *,
    endpoint: str = "https://example.energy.azure.com",
    auth_method: AuthMethod = AuthMethod.USER_IMPERSONATION,
    client_secret: str = "",
    data_partition_id: str = "example-opendes",
) -> ADMEConnection:
    return ADMEConnection(
        endpoint=endpoint,
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id=data_partition_id,
        auth_method=auth_method,
        client_secret=client_secret,
    )


def _patch_method(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    response_factory: Any,
) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return response_factory(**kwargs)

    monkeypatch.setattr(search_module.requests, method, fake)
    return captured


def _patch_get(
    monkeypatch: pytest.MonkeyPatch, response_factory: Any
) -> list[dict[str, Any]]:
    return _patch_method(monkeypatch, "get", response_factory)


def _patch_post(
    monkeypatch: pytest.MonkeyPatch, response_factory: Any
) -> list[dict[str, Any]]:
    return _patch_method(monkeypatch, "post", response_factory)


def _sequential_post(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_FakeResponse | Exception],
) -> list[dict[str, Any]]:
    """Patch ``requests.post`` to return responses in order."""
    captured: list[dict[str, Any]] = []
    iterator = iter(responses)

    def fake(**kwargs: Any) -> Any:
        captured.append(kwargs)
        item = next(iterator)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(search_module.requests, "post", fake)
    return captured


def _hit(
    *,
    record_id: str = "opendes:doc:1",
    kind: str = "osdu:wks:reference-data:1.0.0",
    create_time: str | None = "2024-01-01T00:00:00Z",
    version: int | None = 1,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": record_id,
        "kind": kind,
        "createTime": create_time,
        "version": version,
    }
    if extras:
        payload.update(extras)
    return payload


# ===========================================================================
# Constant regression
# ===========================================================================


def test_search_query_path_constant() -> None:
    assert SEARCH_QUERY_PATH == "/api/search/v2/query"


def test_storage_record_path_template_constant() -> None:
    assert STORAGE_RECORD_PATH_TEMPLATE == "/api/storage/v2/records/{record_id}"
    # Round-trip — pretend we're formatting an id.
    assert (
        STORAGE_RECORD_PATH_TEMPLATE.format(record_id="abc")
        == "/api/storage/v2/records/abc"
    )


def test_wildcard_kind_constant() -> None:
    assert WILDCARD_KIND == "*:*:*:*"


def test_max_offset_plus_limit_constant() -> None:
    assert MAX_OFFSET_PLUS_LIMIT == 10_000


# ===========================================================================
# search_records — happy path & response parsing
# ===========================================================================


def test_search_records_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-search-1"},
            json_payload={
                "results": [_hit(record_id="opendes:doc:1")],
                "totalCount": 1,
            },
        ),
    )

    result = search_records(
        _connection(), token="t", kind="osdu:wks:reference-data:1.0.0"
    )

    assert isinstance(result, SearchPageResult)
    assert result.ok is True
    assert result.http_status == 200
    assert result.kind == "osdu:wks:reference-data:1.0.0"
    assert len(result.records) == 1
    record = result.records[0]
    assert isinstance(record, RecordSummary)
    assert record.id == "opendes:doc:1"
    assert record.kind == "osdu:wks:reference-data:1.0.0"
    assert record.version == 1
    assert result.total_count == 1
    assert result.has_more is False
    assert result.correlation_id == "corr-search-1"
    assert result.latency_ms >= 0.0
    assert captured[0]["url"].endswith(SEARCH_QUERY_PATH)


def test_search_records_body_includes_kind_limit_offset_sort_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(status_code=200, json_payload={"results": []}),
    )
    search_records(
        _connection(),
        token="t",
        kind="osdu:wks:dataset--File.Generic:1.0.0",
        limit=50,
        offset=100,
    )
    body = captured[0]["json"]
    assert body["kind"] == "osdu:wks:dataset--File.Generic:1.0.0"
    assert body["limit"] == 50
    assert body["offset"] == 100
    assert body["sort"] == {"field": ["createTime"], "order": ["DESC"]}
    assert "id" in body["returnedFields"]
    assert "kind" in body["returnedFields"]
    # No query when blank.
    assert "query" not in body


def test_search_records_includes_query_when_non_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(status_code=200, json_payload={"results": []}),
    )
    search_records(
        _connection(), token="t", kind="k", query="  data.foo:bar  "
    )
    assert captured[0]["json"]["query"] == "data.foo:bar"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_search_records_omits_blank_query(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(status_code=200, json_payload={"results": []}),
    )
    search_records(_connection(), token="t", kind="k", query=blank)
    assert "query" not in captured[0]["json"]


# ===========================================================================
# search_records — total_count and has_more
# ===========================================================================


def test_search_records_total_count_present_has_more_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [_hit(record_id=f"id{i}") for i in range(100)],
                "totalCount": 500,
            },
        ),
    )
    result = search_records(
        _connection(), token="t", kind="k", limit=100, offset=0
    )
    assert result.total_count == 500
    assert result.has_more is True  # 0 + 100 < 500


def test_search_records_total_count_present_has_more_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [_hit(record_id=f"id{i}") for i in range(50)],
                "totalCount": 100,
            },
        ),
    )
    result = search_records(
        _connection(), token="t", kind="k", limit=100, offset=50
    )
    # offset 50 + len 50 == 100, not < 100, so no more.
    assert result.has_more is False


def test_search_records_total_count_missing_has_more_from_len_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [_hit(record_id=f"id{i}") for i in range(100)],
            },
        ),
    )
    result = search_records(
        _connection(), token="t", kind="k", limit=100, offset=0
    )
    assert result.total_count is None
    assert result.has_more is True  # len(records) >= limit


def test_search_records_total_count_missing_partial_page_has_more_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [_hit(record_id=f"id{i}") for i in range(20)],
            },
        ),
    )
    result = search_records(
        _connection(), token="t", kind="k", limit=100, offset=0
    )
    assert result.total_count is None
    assert result.has_more is False  # len < limit


def test_search_records_has_more_at_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [_hit(record_id=f"id{i}") for i in range(100)],
                "totalCount": 100_000,
            },
        ),
    )
    # offset 9900 + 100 = 10000 (ceiling). has_more should be calculated
    # against total_count (10000 < 100000 → True). Page may cap further calls.
    result = search_records(
        _connection(), token="t", kind="k", limit=100, offset=9900
    )
    assert result.has_more is True


def test_search_records_inlined_source_fields_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [
                    _hit(
                        record_id="opendes:doc:1",
                        extras={"data": {"foo": "bar"}, "modifyTime": "2024"},
                    )
                ],
            },
        ),
    )
    result = search_records(_connection(), token="t", kind="k")
    record = result.records[0]
    assert record.source["data"] == {"foo": "bar"}
    assert record.source["modifyTime"] == "2024"
    # Promoted fields removed from source.
    assert "id" not in record.source
    assert "kind" not in record.source
    assert "createTime" not in record.source
    assert "version" not in record.source


def test_search_records_explicit_source_block_used_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [
                    _hit(
                        record_id="opendes:doc:1",
                        extras={"source": {"nested": "value"}},
                    )
                ],
            },
        ),
    )
    result = search_records(_connection(), token="t", kind="k")
    assert result.records[0].source == {"nested": "value"}


def test_search_records_skips_malformed_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [
                    {"id": 123, "kind": "k"},  # bad id type
                    {"id": "good", "kind": "k"},
                    {"kind": "k"},  # missing id
                    "not-a-dict",
                ],
            },
        ),
    )
    result = search_records(_connection(), token="t", kind="k")
    assert len(result.records) == 1
    assert result.records[0].id == "good"


def test_search_records_results_not_a_list_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200, json_payload={"results": "nope"}
        ),
    )
    result = search_records(_connection(), token="t", kind="k")
    assert result.ok is True
    assert result.records == []


# ===========================================================================
# search_records — HTTP error paths
# ===========================================================================


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 500])
def test_search_records_http_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=status_code,
            headers={"correlation-id": f"corr-{status_code}"},
            json_payload={"message": f"boom {status_code}"},
        ),
    )
    result = search_records(_connection(), token="t", kind="k")
    assert result.ok is False
    assert result.http_status == status_code
    assert result.records == []
    assert result.error_message is not None
    assert f"boom {status_code}" in result.error_message
    assert result.correlation_id == f"corr-{status_code}"


def test_search_records_400_bad_lucene_query_surfaces_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=400,
            json_payload={"message": "invalid lucene syntax"},
        ),
    )
    result = search_records(
        _connection(), token="t", kind="k", query="data.foo:["
    )
    assert result.ok is False
    assert result.http_status == 400
    assert "invalid lucene syntax" in (result.error_message or "")


def test_search_records_error_falls_back_to_http_when_body_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=500, body="", raise_on_json=True
        ),
    )
    result = search_records(_connection(), token="t", kind="k")
    assert result.ok is False
    assert result.error_message == "HTTP 500"


def test_search_records_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(search_module.requests, "post", fake_post)
    result = search_records(_connection(), token="t", kind="k")
    assert result.ok is False
    assert result.http_status is None
    assert "timed out" in (result.error_message or "").lower()
    assert str(SEARCH_TIMEOUT_SECONDS) in (result.error_message or "")


def test_search_records_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.ConnectionError("dns")

    monkeypatch.setattr(search_module.requests, "post", fake_post)
    result = search_records(_connection(), token="t", kind="k")
    assert result.ok is False
    assert "ConnectionError" in (result.error_message or "")


# ===========================================================================
# search_records — headers + validation
# ===========================================================================


def test_search_records_outgoing_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(status_code=200, json_payload={"results": []}),
    )
    search_records(_connection(), token="bearer-abc", kind="k")
    headers = captured[0]["headers"]
    assert headers["Authorization"] == "Bearer bearer-abc"
    assert headers["data-partition-id"] == "example-opendes"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    assert captured[0]["timeout"] == SEARCH_TIMEOUT_SECONDS
    assert captured[0]["allow_redirects"] is False


@pytest.mark.parametrize(
    "header_name",
    ["correlation-id", "X-Correlation-ID", "Request-Id", "X-Request-Id"],
)
def test_search_records_correlation_id_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, header_name: str
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={header_name: "corr-x"},
            json_payload={"results": []},
        ),
    )
    result = search_records(_connection(), token="t", kind="k")
    assert result.correlation_id == "corr-x"


@pytest.mark.parametrize("kind", ["", "   ", "\t\n"])
def test_search_records_rejects_blank_kind(kind: str) -> None:
    with pytest.raises(ValueError, match="non-empty kind"):
        search_records(_connection(), token="t", kind=kind)


def test_search_records_rejects_zero_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        search_records(_connection(), token="t", kind="k", limit=0)


def test_search_records_rejects_negative_offset() -> None:
    with pytest.raises(ValueError, match="offset must be >= 0"):
        search_records(_connection(), token="t", kind="k", offset=-1)


def test_search_records_rejects_offset_plus_limit_over_ceiling() -> None:
    with pytest.raises(ValueError, match="OSDU Search ceiling"):
        search_records(
            _connection(),
            token="t",
            kind="k",
            limit=DEFAULT_SEARCH_LIMIT,
            offset=MAX_OFFSET_PLUS_LIMIT,
        )


@pytest.mark.parametrize("token", ["", "   ", "\t\n"])
def test_search_records_rejects_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="non-empty bearer token"):
        search_records(_connection(), token=token, kind="k")


def test_search_records_rejects_invalid_connection() -> None:
    bad = ADMEConnection(
        endpoint="", tenant_id="", client_id="", data_partition_id=""
    )
    with pytest.raises(ValueError, match="ADME connection is incomplete"):
        search_records(bad, token="t", kind="k")


# ===========================================================================
# list_kinds
# ===========================================================================


def test_list_kinds_from_aggregation_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-agg"},
            json_payload={
                "aggregations": [
                    {"key": "osdu:wks:reference-data:1.0.0", "count": 10},
                    {"key": "osdu:wks:dataset--File.Generic:1.0.0", "count": 3},
                ],
            },
        ),
    )

    result = list_kinds(_connection(), token="t")
    assert isinstance(result, KindAggregationResult)
    assert result.ok is True
    assert result.from_aggregation is True
    assert result.kinds == [
        "osdu:wks:reference-data:1.0.0",
        "osdu:wks:dataset--File.Generic:1.0.0",
    ]
    assert result.correlation_id == "corr-agg"
    # Aggregation body sent on the first call.
    assert captured[0]["json"]["aggregateBy"] == "kind"
    assert captured[0]["json"]["kind"] == WILDCARD_KIND


def test_list_kinds_aggregation_empty_falls_back_to_page_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _sequential_post(
        monkeypatch,
        [
            _FakeResponse(
                status_code=200, json_payload={"aggregations": []}
            ),
            _FakeResponse(
                status_code=200,
                json_payload={
                    "results": [
                        _hit(record_id="a", kind="osdu:k:b:1"),
                        _hit(record_id="b", kind="osdu:k:a:1"),
                        _hit(record_id="c", kind="osdu:k:b:1"),  # dup
                    ],
                },
            ),
        ],
    )
    result = list_kinds(_connection(), token="t")
    assert result.ok is True
    assert result.from_aggregation is False
    # Sorted unique kinds.
    assert result.kinds == ["osdu:k:a:1", "osdu:k:b:1"]
    assert len(captured) == 2


def test_list_kinds_aggregation_400_falls_back_to_page_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sequential_post(
        monkeypatch,
        [
            _FakeResponse(
                status_code=400,
                json_payload={"message": "aggregation not supported"},
            ),
            _FakeResponse(
                status_code=200,
                json_payload={
                    "results": [_hit(record_id="x", kind="osdu:k:a:1")],
                },
            ),
        ],
    )
    result = list_kinds(_connection(), token="t")
    assert result.ok is True
    assert result.from_aggregation is False
    assert result.kinds == ["osdu:k:a:1"]


def test_list_kinds_aggregation_transport_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sequential_post(
        monkeypatch,
        [
            requests.exceptions.ConnectionError("dns"),
            _FakeResponse(
                status_code=200,
                json_payload={
                    "results": [_hit(record_id="x", kind="osdu:k:a:1")],
                },
            ),
        ],
    )
    result = list_kinds(_connection(), token="t")
    assert result.ok is True
    assert result.from_aggregation is False
    assert result.kinds == ["osdu:k:a:1"]


def test_list_kinds_aggregation_failed_and_fallback_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sequential_post(
        monkeypatch,
        [
            _FakeResponse(
                status_code=400, json_payload={"message": "agg-bad"}
            ),
            _FakeResponse(
                status_code=500, json_payload={"message": "sample-bad"}
            ),
        ],
    )
    result = list_kinds(_connection(), token="t")
    assert result.ok is False
    assert result.from_aggregation is False
    assert result.kinds == []
    assert "sample-bad" in (result.error_message or "")


def test_list_kinds_both_calls_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sequential_post(
        monkeypatch,
        [
            requests.exceptions.Timeout("slow"),
            requests.exceptions.Timeout("slow-2"),
        ],
    )
    result = list_kinds(_connection(), token="t")
    assert result.ok is False
    assert result.from_aggregation is False
    assert result.kinds == []
    assert result.error_message is not None


def test_list_kinds_outgoing_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={"aggregations": [{"key": "k1"}]},
        ),
    )
    list_kinds(_connection(), token="bearer-abc")
    headers = captured[0]["headers"]
    assert headers["Authorization"] == "Bearer bearer-abc"
    assert headers["data-partition-id"] == "example-opendes"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"


@pytest.mark.parametrize(
    "header_name",
    ["correlation-id", "X-Correlation-ID", "Request-Id", "X-Request-Id"],
)
def test_list_kinds_correlation_id_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, header_name: str
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={header_name: "corr-x"},
            json_payload={"aggregations": [{"key": "k"}]},
        ),
    )
    result = list_kinds(_connection(), token="t")
    assert result.correlation_id == "corr-x"


@pytest.mark.parametrize("token", ["", "   ", "\t\n"])
def test_list_kinds_rejects_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="non-empty bearer token"):
        list_kinds(_connection(), token=token)


def test_list_kinds_rejects_invalid_connection() -> None:
    bad = ADMEConnection(
        endpoint="", tenant_id="", client_id="", data_partition_id=""
    )
    with pytest.raises(ValueError, match="ADME connection is incomplete"):
        list_kinds(bad, token="t")


# ===========================================================================
# get_record
# ===========================================================================


def test_get_record_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_get(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-rec"},
            json_payload={"id": "opendes:doc:1", "kind": "k", "data": {"x": 1}},
        ),
    )
    result = get_record(_connection(), token="t", record_id="opendes:doc:1")
    assert isinstance(result, RecordDetailResult)
    assert result.ok is True
    assert result.http_status == 200
    assert result.record == {
        "id": "opendes:doc:1",
        "kind": "k",
        "data": {"x": 1},
    }
    assert result.correlation_id == "corr-rec"
    # URL preserves colons (the OSDU id separator) and uses the template.
    assert captured[0]["url"].endswith("/api/storage/v2/records/opendes:doc:1")


def test_get_record_url_encodes_unsafe_chars_preserving_colons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_get(
        monkeypatch,
        lambda **_: _FakeResponse(status_code=200, json_payload={}),
    )
    weird = "opendes:doc/with space:1"
    get_record(_connection(), token="t", record_id=weird)
    expected = quote(weird, safe=":")
    assert captured[0]["url"].endswith(
        f"/api/storage/v2/records/{expected}"
    )
    # Colons preserved.
    assert ":" in captured[0]["url"].split("/records/")[-1]


def test_get_record_404_returns_friendly_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_get(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=404,
            headers={"correlation-id": "corr-404"},
            json_payload={"message": "not found"},
        ),
    )
    result = get_record(_connection(), token="t", record_id="opendes:doc:missing")
    assert result.ok is False
    assert result.http_status == 404
    assert result.record is None
    assert result.error_message is not None
    assert "opendes:doc:missing" in result.error_message
    assert "not found or not visible" in result.error_message


@pytest.mark.parametrize("status_code", [400, 401, 403, 500])
def test_get_record_other_http_errors_propagate_body_message(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _patch_get(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=status_code,
            json_payload={"message": f"boom {status_code}"},
        ),
    )
    result = get_record(_connection(), token="t", record_id="opendes:doc:1")
    assert result.ok is False
    assert result.http_status == status_code
    assert result.record is None
    assert f"boom {status_code}" in (result.error_message or "")


def test_get_record_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(**_: Any) -> Any:
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(search_module.requests, "get", fake_get)
    result = get_record(_connection(), token="t", record_id="opendes:doc:1")
    assert result.ok is False
    assert result.http_status is None
    assert "timed out" in (result.error_message or "").lower()


def test_get_record_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(**_: Any) -> Any:
        raise requests.exceptions.ConnectionError("dns")

    monkeypatch.setattr(search_module.requests, "get", fake_get)
    result = get_record(_connection(), token="t", record_id="opendes:doc:1")
    assert result.ok is False
    assert "ConnectionError" in (result.error_message or "")


def test_get_record_outgoing_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_get(
        monkeypatch,
        lambda **_: _FakeResponse(status_code=200, json_payload={}),
    )
    get_record(_connection(), token="bearer-abc", record_id="opendes:doc:1")
    headers = captured[0]["headers"]
    assert headers["Authorization"] == "Bearer bearer-abc"
    assert headers["data-partition-id"] == "example-opendes"
    assert headers["Accept"] == "application/json"
    # GET has no body — no Content-Type.
    assert "Content-Type" not in headers
    assert captured[0]["timeout"] == SEARCH_TIMEOUT_SECONDS
    assert captured[0]["allow_redirects"] is False


@pytest.mark.parametrize(
    "header_name",
    ["correlation-id", "X-Correlation-ID", "Request-Id", "X-Request-Id"],
)
def test_get_record_correlation_id_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, header_name: str
) -> None:
    _patch_get(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={header_name: "corr-x"},
            json_payload={},
        ),
    )
    result = get_record(_connection(), token="t", record_id="opendes:doc:1")
    assert result.correlation_id == "corr-x"


@pytest.mark.parametrize("record_id", ["", "   ", "\t\n"])
def test_get_record_rejects_blank_record_id(record_id: str) -> None:
    with pytest.raises(ValueError, match="non-empty record_id"):
        get_record(_connection(), token="t", record_id=record_id)


@pytest.mark.parametrize("token", ["", "   ", "\t\n"])
def test_get_record_rejects_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="non-empty bearer token"):
        get_record(_connection(), token=token, record_id="opendes:doc:1")


def test_get_record_rejects_invalid_connection() -> None:
    bad = ADMEConnection(
        endpoint="", tenant_id="", client_id="", data_partition_id=""
    )
    with pytest.raises(ValueError, match="ADME connection is incomplete"):
        get_record(bad, token="t", record_id="opendes:doc:1")


def test_get_record_non_dict_body_returns_none_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_get(
        monkeypatch,
        lambda **_: _FakeResponse(status_code=200, json_payload=None),
    )
    result = get_record(_connection(), token="t", record_id="opendes:doc:1")
    assert result.ok is True
    assert result.record is None


# ===========================================================================
# search_with_cursor — constants
# ===========================================================================


def test_search_cursor_query_path_constant() -> None:
    assert SEARCH_CURSOR_QUERY_PATH == "/api/search/v2/query_with_cursor"


def test_export_timeout_seconds_constant() -> None:
    assert EXPORT_TIMEOUT_SECONDS == 30


# ===========================================================================
# search_with_cursor — happy path
# ===========================================================================


def test_cursor_search_first_page_omits_cursor_from_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-cursor-1"},
            json_payload={
                "cursor": "abc123",
                "results": [_hit(record_id="opendes:doc:1")],
                "totalCount": 500,
            },
        ),
    )
    result = search_with_cursor(
        _connection(),
        token="t",
        kind="osdu:wks:reference-data:1.0.0",
    )

    assert isinstance(result, CursorSearchResult)
    assert result.ok is True
    assert result.http_status == 200
    assert result.cursor == "abc123"
    assert result.has_more is True
    assert result.total_count == 500
    assert len(result.records) == 1
    assert result.records[0].id == "opendes:doc:1"
    assert result.correlation_id == "corr-cursor-1"
    # cursor must NOT be in request body on first call
    assert "cursor" not in captured[0]["json"]
    assert captured[0]["url"].endswith(SEARCH_CURSOR_QUERY_PATH)


def test_cursor_search_continuation_includes_cursor_in_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "cursor": "def456",
                "results": [_hit(record_id="opendes:doc:2")],
                "totalCount": 500,
            },
        ),
    )
    result = search_with_cursor(
        _connection(),
        token="t",
        kind="k",
        cursor="abc123",
    )
    assert result.ok is True
    assert result.cursor == "def456"
    assert result.has_more is True
    assert captured[0]["json"]["cursor"] == "abc123"


def test_cursor_search_last_page_has_more_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "cursor": "",
                "results": [_hit(record_id="opendes:doc:last")],
                "totalCount": 5,
            },
        ),
    )
    result = search_with_cursor(
        _connection(), token="t", kind="k", cursor="prev-cursor"
    )
    assert result.ok is True
    assert result.cursor is None  # empty string normalised to None
    assert result.has_more is False
    assert len(result.records) == 1


def test_cursor_search_null_cursor_has_more_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "cursor": None,
                "results": [],
                "totalCount": 0,
            },
        ),
    )
    result = search_with_cursor(
        _connection(), token="t", kind="k"
    )
    assert result.ok is True
    assert result.has_more is False
    assert result.cursor is None


# ===========================================================================
# search_with_cursor — returned_fields override
# ===========================================================================


def test_cursor_search_uses_default_returned_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200, json_payload={"results": []}
        ),
    )
    search_with_cursor(_connection(), token="t", kind="k")
    body = captured[0]["json"]
    assert "id" in body["returnedFields"]
    assert "kind" in body["returnedFields"]


def test_cursor_search_returned_fields_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200, json_payload={"results": []}
        ),
    )
    custom_fields = ("id", "kind", "data.*")
    search_with_cursor(
        _connection(),
        token="t",
        kind="k",
        returned_fields=custom_fields,
    )
    body = captured[0]["json"]
    assert body["returnedFields"] == ["id", "kind", "data.*"]


# ===========================================================================
# search_with_cursor — uses EXPORT_TIMEOUT_SECONDS
# ===========================================================================


def test_cursor_search_uses_export_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200, json_payload={"results": []}
        ),
    )
    search_with_cursor(_connection(), token="t", kind="k")
    assert captured[0]["timeout"] == EXPORT_TIMEOUT_SECONDS


# ===========================================================================
# search_with_cursor — error handling
# ===========================================================================


def test_cursor_search_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.ConnectionError("dns")

    monkeypatch.setattr(search_module.requests, "post", fake_post)
    result = search_with_cursor(_connection(), token="t", kind="k")
    assert result.ok is False
    assert result.http_status is None
    assert "ConnectionError" in (result.error_message or "")


def test_cursor_search_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(search_module.requests, "post", fake_post)
    result = search_with_cursor(_connection(), token="t", kind="k")
    assert result.ok is False
    assert "timed out" in (result.error_message or "").lower()
    assert str(EXPORT_TIMEOUT_SECONDS) in (result.error_message or "")


def test_cursor_search_http_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=400,
            json_payload={"message": "bad query syntax"},
        ),
    )
    result = search_with_cursor(
        _connection(), token="t", kind="k", query="data.foo:["
    )
    assert result.ok is False
    assert result.http_status == 400
    assert "bad query syntax" in (result.error_message or "")


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_cursor_search_other_http_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=status_code,
            headers={"correlation-id": f"corr-{status_code}"},
            json_payload={"message": f"err {status_code}"},
        ),
    )
    result = search_with_cursor(_connection(), token="t", kind="k")
    assert result.ok is False
    assert result.http_status == status_code
    assert result.records == []
    assert result.correlation_id == f"corr-{status_code}"


def test_cursor_search_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "cursor": None,
                "results": [],
                "totalCount": 0,
            },
        ),
    )
    result = search_with_cursor(_connection(), token="t", kind="k")
    assert result.ok is True
    assert result.records == []
    assert result.has_more is False


# ===========================================================================
# search_with_cursor — validation
# ===========================================================================


@pytest.mark.parametrize("kind", ["", "   ", "\t\n"])
def test_cursor_search_rejects_blank_kind(kind: str) -> None:
    with pytest.raises(ValueError, match="non-empty kind"):
        search_with_cursor(_connection(), token="t", kind=kind)


def test_cursor_search_rejects_zero_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        search_with_cursor(_connection(), token="t", kind="k", limit=0)


@pytest.mark.parametrize("token", ["", "   ", "\t\n"])
def test_cursor_search_rejects_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="non-empty bearer token"):
        search_with_cursor(_connection(), token=token, kind="k")


# ===========================================================================
# search_with_cursor — query handling
# ===========================================================================


def test_cursor_search_includes_query_when_non_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200, json_payload={"results": []}
        ),
    )
    search_with_cursor(
        _connection(), token="t", kind="k", query="  data.foo:bar  "
    )
    assert captured[0]["json"]["query"] == "data.foo:bar"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_cursor_search_omits_blank_query(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200, json_payload={"results": []}
        ),
    )
    search_with_cursor(_connection(), token="t", kind="k", query=blank)
    assert "query" not in captured[0]["json"]


# ===========================================================================
# export_all_records — multi-page iteration
# ===========================================================================


def test_export_all_records_multi_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sequential_post(
        monkeypatch,
        [
            _FakeResponse(
                status_code=200,
                json_payload={
                    "cursor": "c1",
                    "results": [_hit(record_id="r1")],
                    "totalCount": 3,
                },
            ),
            _FakeResponse(
                status_code=200,
                json_payload={
                    "cursor": "c2",
                    "results": [_hit(record_id="r2")],
                    "totalCount": 3,
                },
            ),
            _FakeResponse(
                status_code=200,
                json_payload={
                    "cursor": None,
                    "results": [_hit(record_id="r3")],
                    "totalCount": 3,
                },
            ),
        ],
    )
    pages = list(
        export_all_records(
            _connection(), token="t", kind="k"
        )
    )
    assert len(pages) == 3
    assert all(isinstance(p, CursorSearchResult) for p in pages)
    assert pages[0].has_more is True
    assert pages[1].has_more is True
    assert pages[2].has_more is False
    assert pages[0].records[0].id == "r1"
    assert pages[1].records[0].id == "r2"
    assert pages[2].records[0].id == "r3"


def test_export_all_records_single_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "cursor": None,
                "results": [_hit(record_id="only")],
                "totalCount": 1,
            },
        ),
    )
    pages = list(
        export_all_records(
            _connection(), token="t", kind="k"
        )
    )
    assert len(pages) == 1
    assert pages[0].ok is True
    assert pages[0].has_more is False


# ===========================================================================
# export_all_records — stops on error
# ===========================================================================


def test_export_all_records_stops_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sequential_post(
        monkeypatch,
        [
            _FakeResponse(
                status_code=200,
                json_payload={
                    "cursor": "c1",
                    "results": [_hit(record_id="r1")],
                    "totalCount": 100,
                },
            ),
            _FakeResponse(
                status_code=500,
                json_payload={"message": "internal"},
            ),
        ],
    )
    pages = list(
        export_all_records(
            _connection(), token="t", kind="k"
        )
    )
    assert len(pages) == 2
    assert pages[0].ok is True
    assert pages[1].ok is False
    assert pages[1].http_status == 500


def test_export_all_records_stops_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sequential_post(
        monkeypatch,
        [
            _FakeResponse(
                status_code=200,
                json_payload={
                    "cursor": "c1",
                    "results": [_hit(record_id="r1")],
                    "totalCount": 100,
                },
            ),
            requests.exceptions.ConnectionError("dns"),
        ],
    )
    pages = list(
        export_all_records(
            _connection(), token="t", kind="k"
        )
    )
    assert len(pages) == 2
    assert pages[0].ok is True
    assert pages[1].ok is False
    assert "ConnectionError" in (pages[1].error_message or "")


# ===========================================================================
# export_all_records — passes returned_fields through
# ===========================================================================


def test_export_all_records_passes_returned_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "cursor": None,
                "results": [],
                "totalCount": 0,
            },
        ),
    )
    custom_fields = ("id", "kind", "data.*")
    list(
        export_all_records(
            _connection(),
            token="t",
            kind="k",
            returned_fields=custom_fields,
        )
    )
    assert captured[0]["json"]["returnedFields"] == ["id", "kind", "data.*"]


# ===========================================================================
# build_multi_kind_query
# ===========================================================================


def test_build_multi_kind_query_single_kind() -> None:
    kind, query = build_multi_kind_query(
        ["osdu:wks:master-data--Well:1.0.0"]
    )
    assert kind == "osdu:wks:master-data--Well:1.0.0"
    assert query == ""


def test_build_multi_kind_query_single_kind_with_base_query() -> None:
    kind, query = build_multi_kind_query(
        ["osdu:wks:master-data--Well:1.0.0"],
        base_query="data.Name:\"Test\"",
    )
    assert kind == "osdu:wks:master-data--Well:1.0.0"
    assert query == 'data.Name:"Test"'


def test_build_multi_kind_query_multiple_kinds() -> None:
    kind, query = build_multi_kind_query(
        [
            "osdu:wks:master-data--Well:1.0.0",
            "osdu:wks:master-data--Wellbore:1.0.0",
        ]
    )
    assert kind == WILDCARD_KIND
    assert 'kind:"osdu:wks:master-data--Well:1.0.0"' in query
    assert 'kind:"osdu:wks:master-data--Wellbore:1.0.0"' in query
    assert " OR " in query
    assert query.startswith("(")
    assert query.endswith(")")


def test_build_multi_kind_query_multiple_kinds_with_base_query() -> None:
    kind, query = build_multi_kind_query(
        [
            "osdu:wks:master-data--Well:1.0.0",
            "osdu:wks:master-data--Wellbore:1.0.0",
        ],
        base_query="data.Name:\"Test\"",
    )
    assert kind == WILDCARD_KIND
    assert " AND " in query
    assert 'data.Name:"Test"' in query
    assert 'kind:"osdu:wks:master-data--Well:1.0.0"' in query
    assert 'kind:"osdu:wks:master-data--Wellbore:1.0.0"' in query


def test_build_multi_kind_query_empty_list() -> None:
    kind, query = build_multi_kind_query([])
    assert kind == WILDCARD_KIND
    assert query == ""


def test_build_multi_kind_query_empty_list_with_base_query() -> None:
    kind, query = build_multi_kind_query([], base_query="data.x:1")
    assert kind == WILDCARD_KIND
    assert query == "data.x:1"


def test_build_multi_kind_query_base_query_none() -> None:
    kind, query = build_multi_kind_query(
        ["osdu:wks:master-data--Well:1.0.0"], base_query=None
    )
    assert kind == "osdu:wks:master-data--Well:1.0.0"
    assert query == ""


def test_build_multi_kind_query_base_query_whitespace() -> None:
    kind, query = build_multi_kind_query(
        ["osdu:wks:master-data--Well:1.0.0"], base_query="   "
    )
    assert kind == "osdu:wks:master-data--Well:1.0.0"
    assert query == ""


# ===========================================================================
# search_with_aggregation — happy path
# ===========================================================================


def test_search_with_aggregation_happy_path_with_aggregate_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-agg-1"},
            json_payload={
                "results": [_hit(record_id="opendes:doc:1")],
                "totalCount": 42,
                "aggregations": [
                    {"key": "osdu:wks:reference-data:1.0.0", "count": 30},
                    {"key": "osdu:wks:dataset--File.Generic:1.0.0", "count": 12},
                ],
            },
        ),
    )

    result = search_with_aggregation(
        _connection(),
        token="t",
        kind=WILDCARD_KIND,
        aggregate_by="kind",
    )

    assert isinstance(result, SearchAggregationResult)
    assert result.ok is True
    assert result.http_status == 200
    assert result.kind == WILDCARD_KIND
    assert len(result.records) == 1
    assert result.records[0].id == "opendes:doc:1"
    assert result.total_count == 42
    assert result.correlation_id == "corr-agg-1"
    assert len(result.aggregations) == 2
    assert result.aggregations[0] == AggregationBucket(
        key="osdu:wks:reference-data:1.0.0", count=30
    )
    assert result.aggregations[1] == AggregationBucket(
        key="osdu:wks:dataset--File.Generic:1.0.0", count=12
    )
    # Body must include aggregateBy
    assert captured[0]["json"]["aggregateBy"] == "kind"
    assert captured[0]["url"].endswith(SEARCH_QUERY_PATH)


def test_search_with_aggregation_happy_path_without_aggregate_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-no-agg"},
            json_payload={
                "results": [
                    _hit(record_id="opendes:doc:1"),
                    _hit(record_id="opendes:doc:2"),
                ],
                "totalCount": 2,
            },
        ),
    )

    result = search_with_aggregation(
        _connection(),
        token="t",
        kind="osdu:wks:reference-data:1.0.0",
    )

    assert result.ok is True
    assert result.records[0].id == "opendes:doc:1"
    assert result.records[1].id == "opendes:doc:2"
    assert result.total_count == 2
    assert result.aggregations == []
    # aggregateBy must NOT be in body
    assert "aggregateBy" not in captured[0]["json"]


def test_search_with_aggregation_body_includes_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200, json_payload={"results": [], "totalCount": 0}
        ),
    )
    search_with_aggregation(
        _connection(),
        token="t",
        kind="k",
        query="  data.foo:bar  ",
        aggregate_by="kind",
    )
    body = captured[0]["json"]
    assert body["query"] == "data.foo:bar"
    assert body["aggregateBy"] == "kind"


def test_search_with_aggregation_omits_blank_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200, json_payload={"results": [], "totalCount": 0}
        ),
    )
    search_with_aggregation(
        _connection(), token="t", kind="k", query="   "
    )
    assert "query" not in captured[0]["json"]


# ===========================================================================
# search_with_aggregation — aggregation parsing
# ===========================================================================


def test_aggregation_parsing_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [],
                "totalCount": 0,
                "aggregations": [],
            },
        ),
    )
    result = search_with_aggregation(
        _connection(), token="t", kind="k", aggregate_by="kind"
    )
    assert result.ok is True
    assert result.aggregations == []


def test_aggregation_parsing_no_aggregations_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={"results": [], "totalCount": 0},
        ),
    )
    result = search_with_aggregation(
        _connection(), token="t", kind="k", aggregate_by="kind"
    )
    assert result.ok is True
    assert result.aggregations == []


def test_aggregation_parsing_malformed_entries_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [],
                "totalCount": 0,
                "aggregations": [
                    {"key": "good-kind", "count": 5},
                    {"key": "", "count": 3},       # empty key
                    {"key": "no-count"},             # missing count
                    "not-a-dict",                    # wrong type
                    {"key": 123, "count": 1},        # non-string key
                    {"key": "bool-count", "count": True},  # bool count
                    {"key": "valid-too", "count": 0},
                ],
            },
        ),
    )
    result = search_with_aggregation(
        _connection(), token="t", kind="k", aggregate_by="kind"
    )
    assert result.ok is True
    assert len(result.aggregations) == 2
    assert result.aggregations[0] == AggregationBucket(key="good-kind", count=5)
    assert result.aggregations[1] == AggregationBucket(key="valid-too", count=0)


def test_aggregation_parsing_aggregations_not_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [],
                "totalCount": 0,
                "aggregations": "nope",
            },
        ),
    )
    result = search_with_aggregation(
        _connection(), token="t", kind="k", aggregate_by="kind"
    )
    assert result.ok is True
    assert result.aggregations == []


# ===========================================================================
# search_with_aggregation — has_more
# ===========================================================================


def test_search_with_aggregation_has_more_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [_hit(record_id=f"id{i}") for i in range(100)],
                "totalCount": 500,
            },
        ),
    )
    result = search_with_aggregation(
        _connection(), token="t", kind="k", limit=100, offset=0
    )
    assert result.has_more is True


def test_search_with_aggregation_has_more_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "results": [_hit(record_id=f"id{i}") for i in range(10)],
                "totalCount": 10,
            },
        ),
    )
    result = search_with_aggregation(
        _connection(), token="t", kind="k", limit=100, offset=0
    )
    assert result.has_more is False


# ===========================================================================
# search_with_aggregation — error paths
# ===========================================================================


@pytest.mark.parametrize("status_code", [400, 401, 403, 500])
def test_search_with_aggregation_http_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=status_code,
            headers={"correlation-id": f"corr-{status_code}"},
            json_payload={"message": f"boom {status_code}"},
        ),
    )
    result = search_with_aggregation(
        _connection(), token="t", kind="k", aggregate_by="kind"
    )
    assert result.ok is False
    assert result.http_status == status_code
    assert result.records == []
    assert result.aggregations == []
    assert f"boom {status_code}" in (result.error_message or "")
    assert result.correlation_id == f"corr-{status_code}"


def test_search_with_aggregation_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(search_module.requests, "post", fake_post)
    result = search_with_aggregation(
        _connection(), token="t", kind="k", aggregate_by="kind"
    )
    assert result.ok is False
    assert result.http_status is None
    assert "timed out" in (result.error_message or "").lower()


def test_search_with_aggregation_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.ConnectionError("dns")

    monkeypatch.setattr(search_module.requests, "post", fake_post)
    result = search_with_aggregation(
        _connection(), token="t", kind="k"
    )
    assert result.ok is False
    assert "ConnectionError" in (result.error_message or "")


# ===========================================================================
# search_with_aggregation — validation
# ===========================================================================


@pytest.mark.parametrize("kind", ["", "   ", "\t\n"])
def test_search_with_aggregation_rejects_blank_kind(kind: str) -> None:
    with pytest.raises(ValueError, match="non-empty kind"):
        search_with_aggregation(_connection(), token="t", kind=kind)


def test_search_with_aggregation_rejects_zero_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        search_with_aggregation(
            _connection(), token="t", kind="k", limit=0
        )


def test_search_with_aggregation_rejects_negative_offset() -> None:
    with pytest.raises(ValueError, match="offset must be >= 0"):
        search_with_aggregation(
            _connection(), token="t", kind="k", offset=-1
        )


def test_search_with_aggregation_rejects_offset_plus_limit_over_ceiling() -> None:
    with pytest.raises(ValueError, match="OSDU Search ceiling"):
        search_with_aggregation(
            _connection(),
            token="t",
            kind="k",
            limit=DEFAULT_SEARCH_LIMIT,
            offset=MAX_OFFSET_PLUS_LIMIT,
        )


@pytest.mark.parametrize("token", ["", "   ", "\t\n"])
def test_search_with_aggregation_rejects_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="non-empty bearer token"):
        search_with_aggregation(_connection(), token=token, kind="k")


def test_search_with_aggregation_rejects_invalid_connection() -> None:
    bad = ADMEConnection(
        endpoint="", tenant_id="", client_id="", data_partition_id=""
    )
    with pytest.raises(ValueError, match="ADME connection is incomplete"):
        search_with_aggregation(bad, token="t", kind="k")
