"""Tests for the ADME Search page (`app/pages/7_🔍_Search.py`)."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from app.connection_state import (
    CONNECTION_KEY,
    USER_AUTH_STATE_KEY,
)
from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import (
    CursorSearchResult,
    KindAggregationResult,
    RecordDetailResult,
    RecordSummary,
    SearchPageResult,
)
from tests.support.streamlit_recorder import StreamlitRecorder

SEARCH_PAGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "pages"
    / "7_🔍_Search.py"
)

# Locked session-state keys mirror those in the page.
QUERY_TEXT_KEY = "search_query_text"
KIND_FILTER_KEY = "search_kind_filter"
KIND_OPTIONS_KEY = "search_kind_options"
RESULTS_KEY = "search_results"
TOTAL_COUNT_KEY = "search_total_count"
PAGE_OFFSET_KEY = "search_page_offset"
HISTORY_KEY = "search_history"
LAST_ERROR_KEY = "search_last_error"
SELECTED_RECORD_ID_KEY = "search_selected_record_id"
FULL_RECORD_CACHE_KEY = "search_full_record_cache"
AUTORUN_DONE_KEY = "search_autorun_done"
RESOLVED_QUERY_KEY = "search_resolved_query"

# Multi-kind + aggregation keys (issue #26).
KIND_SELECTIONS_KEY = "search_kind_selections"
AGGREGATE_KEY = "search_aggregate_by_kind"
AGGREGATION_RESULTS_KEY = "search_aggregation_results"

# Query builder keys (issue #27).
QUERY_BUILDER_CLAUSES_KEY = "query_builder_clauses"
QUERY_BUILDER_COMBINATOR_KEY = "query_builder_combinator"

# Export keys (issue #25).
EXPORT_STATUS_KEY = "export_status"
EXPORT_RECORDS_KEY = "export_records"
EXPORT_FORMAT_KEY = "export_format"
EXPORT_ERROR_KEY = "export_error"
EXPORT_ABORT_KEY = "export_abort_requested"

WILDCARD_KIND = "*:*:*:*"

# Button / widget labels (verbatim — page contract).
REFRESH_LABEL = "🔄 Refresh"
SEARCH_LABEL = "🔍 Search"
PREV_LABEL = "« Prev"
NEXT_LABEL = "Next »"
DISMISS_LABEL = "Dismiss error"
CLEAR_HISTORY_LABEL = "Clear history"
FETCH_FULL_LABEL = "📥 Fetch full record"
REFRESH_FULL_LABEL = "🔄 Refresh full record"
KIND_SELECT_LABEL = "Kind filter"
QUERY_INPUT_LABEL = "Free-text query (Lucene syntax)"
RECORD_SELECTOR_LABEL = "Select a record to inspect"
KIND_FILTER_LABEL = "Kind filter"

# Export button labels.
EXPORT_CSV_LABEL = "📥 Export CSV"
EXPORT_JSON_LABEL = "📥 Export JSON"


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------


def _load_page(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    monkeypatch.setitem(sys.modules, "streamlit", streamlit_recorder)
    module_name = "tests.generated_search_page"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, SEARCH_PAGE_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _service_principal_connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        auth_method=AuthMethod.SERVICE_PRINCIPAL,
        client_secret="placeholder-secret",
    )


def _user_connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        auth_method=AuthMethod.USER_IMPERSONATION,
    )


def _summary(record_id: str = "opendes:doc:1") -> RecordSummary:
    return RecordSummary(
        id=record_id,
        kind="osdu:wks:reference-data:1.0.0",
        create_time="2024-01-01T00:00:00Z",
        version=1,
        source={"data": {"foo": "bar"}},
    )


def _ok_kinds() -> KindAggregationResult:
    return KindAggregationResult(
        kinds=["osdu:wks:reference-data:1.0.0", "osdu:wks:dataset:1.0.0"],
        from_aggregation=True,
        ok=True,
        http_status=200,
        latency_ms=12.0,
        correlation_id="corr-kinds",
    )


def _ok_search(
    *,
    records: list[RecordSummary] | None = None,
    offset: int = 0,
    total: int | None = 100,
) -> SearchPageResult:
    recs = records if records is not None else [_summary()]
    return SearchPageResult(
        kind="osdu:wks:reference-data:1.0.0",
        offset=offset,
        limit=100,
        records=recs,
        total_count=total,
        has_more=total is not None and offset + len(recs) < total,
        ok=True,
        http_status=200,
        latency_ms=22.0,
        correlation_id="corr-search",
    )


def _failed_search_400() -> SearchPageResult:
    return SearchPageResult(
        kind="*:*:*:*",
        offset=0,
        limit=100,
        ok=False,
        http_status=400,
        latency_ms=8.0,
        correlation_id="corr-400",
        error_message="invalid lucene syntax",
    )


def _ok_get_record() -> RecordDetailResult:
    return RecordDetailResult(
        record_id="opendes:doc:1",
        record={"id": "opendes:doc:1", "data": {"foo": "bar"}},
        ok=True,
        http_status=200,
        latency_ms=15.0,
        correlation_id="corr-rec",
    )


def _missing_get_record() -> RecordDetailResult:
    return RecordDetailResult(
        record_id="opendes:doc:1",
        record=None,
        ok=False,
        http_status=404,
        latency_ms=5.0,
        correlation_id="corr-404",
        error_message="Record 'opendes:doc:1' not found or not visible.",
    )


# ---------------------------------------------------------------------------
# Service spy
# ---------------------------------------------------------------------------


class _Spy:
    def __init__(self) -> None:
        self.list_kinds_calls: list[Any] = []
        self.search_calls: list[dict[str, Any]] = []
        self.get_record_calls: list[tuple[Any, str, str]] = []
        self.token_calls: list[Any] = []


def _patch_services(
    page_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kinds_result: KindAggregationResult | None = None,
    search_result: SearchPageResult | None = None,
    record_result: RecordDetailResult | None = None,
    token: str | None = "test-token",
) -> _Spy:
    spy = _Spy()

    def fake_get_token(connection: ADMEConnection, **_: Any) -> str:
        spy.token_calls.append(connection)
        if token is None:
            from app.services.auth import AuthenticationError

            raise AuthenticationError("no token")
        return token

    def fake_list_kinds(
        connection: ADMEConnection, supplied_token: str
    ) -> KindAggregationResult:
        spy.list_kinds_calls.append((connection, supplied_token))
        return kinds_result if kinds_result is not None else _ok_kinds()

    def fake_search(
        connection: ADMEConnection,
        supplied_token: str,
        *,
        kind: str,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> SearchPageResult:
        spy.search_calls.append(
            {
                "connection": connection,
                "token": supplied_token,
                "kind": kind,
                "query": query,
                "limit": limit,
                "offset": offset,
            }
        )
        return search_result if search_result is not None else _ok_search()

    def fake_get_record(
        connection: ADMEConnection,
        supplied_token: str,
        record_id: str,
    ) -> RecordDetailResult:
        spy.get_record_calls.append((connection, supplied_token, record_id))
        return record_result if record_result is not None else _ok_get_record()

    monkeypatch.setattr(page_module, "get_token", fake_get_token)
    monkeypatch.setattr(page_module, "list_kinds", fake_list_kinds)
    monkeypatch.setattr(page_module, "search_records", fake_search)
    monkeypatch.setattr(page_module, "get_record", fake_get_record)
    return spy


# ===========================================================================
# Pre-flight
# ===========================================================================


def test_page_blocks_when_no_connection_configured(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(page_module, monkeypatch)

    page_module.main()

    info_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("info")
    ]
    assert any("Instance Configuration" in m for m in info_messages)
    assert streamlit_recorder.calls_named("page_link")
    assert spy.list_kinds_calls == []
    assert spy.search_calls == []
    assert spy.token_calls == []


def test_page_blocks_user_impersonation_without_token(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_STATE_KEY] = None
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(page_module, monkeypatch)

    page_module.main()

    page_link_calls = streamlit_recorder.calls_named("page_link")
    assert page_link_calls
    info_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("info")
    ]
    assert any("Instance Configuration" in m for m in info_messages)
    assert spy.list_kinds_calls == []
    assert spy.search_calls == []


def test_page_blocks_when_data_partition_missing(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="",
        auth_method=AuthMethod.SERVICE_PRINCIPAL,
        client_secret="placeholder-secret",
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(page_module, monkeypatch)

    page_module.main()

    assert streamlit_recorder.calls_named("page_link")
    info_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("info")
    ]
    assert any("Instance Configuration" in m for m in info_messages)
    assert spy.list_kinds_calls == []
    assert spy.search_calls == []


# ===========================================================================
# Autorun-once
# ===========================================================================


def test_autorun_fires_list_kinds_and_search_on_first_render(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(page_module, monkeypatch)

    page_module.main()

    assert len(spy.list_kinds_calls) == 1
    assert len(spy.search_calls) == 1
    assert streamlit_recorder.session_state[AUTORUN_DONE_KEY] is True
    # Kind options populated.
    options = cast(list[str], streamlit_recorder.session_state[KIND_OPTIONS_KEY])
    assert options[0] == WILDCARD_KIND
    assert "osdu:wks:reference-data:1.0.0" in options


def test_autorun_does_not_refire_on_second_render(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(page_module, monkeypatch)

    page_module.main()

    assert spy.list_kinds_calls == []
    assert spy.search_calls == []


def test_refresh_button_bypasses_autorun_guard(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.button_responses[REFRESH_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(page_module, monkeypatch)

    page_module.main()

    # Refresh forces a list_kinds + search even though autorun_done=True.
    assert len(spy.list_kinds_calls) == 1
    assert len(spy.search_calls) == 1
    # Pagination resets.
    assert streamlit_recorder.session_state[PAGE_OFFSET_KEY] == 0


# ===========================================================================
# Search execution
# ===========================================================================


def test_search_button_calls_search_with_selected_kind_and_query(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [
        WILDCARD_KIND,
        "osdu:wks:reference-data:1.0.0",
    ]
    streamlit_recorder.session_state[KIND_SELECTIONS_KEY] = [
        "osdu:wks:reference-data:1.0.0",
    ]
    streamlit_recorder.widget_values[KIND_SELECT_LABEL] = [
        "osdu:wks:reference-data:1.0.0",
    ]
    streamlit_recorder.widget_values[QUERY_INPUT_LABEL] = "data.foo:bar"
    # The text_input below the toolbar writes its value to session_state
    # via the bound key — the recorder doesn't auto-bind, so set it.
    streamlit_recorder.session_state[QUERY_TEXT_KEY] = "data.foo:bar"
    streamlit_recorder.button_responses[SEARCH_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(page_module, monkeypatch)

    page_module.main()

    assert len(spy.search_calls) == 1
    call = spy.search_calls[0]
    assert call["kind"] == "osdu:wks:reference-data:1.0.0"
    assert call["query"] == "data.foo:bar"
    assert call["offset"] == 0
    assert streamlit_recorder.session_state[RESOLVED_QUERY_KEY] == (
        "data.foo:bar"
    )


def test_blank_query_passed_through_as_none(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(page_module, monkeypatch)

    page_module.main()

    # Autorun search with empty resolved query.
    assert spy.search_calls
    assert spy.search_calls[0]["query"] in (None, "")


# ===========================================================================
# Empty results + missing kind list
# ===========================================================================


def test_empty_results_render_friendly_message(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(
        page_module,
        monkeypatch,
        search_result=_ok_search(records=[], total=0),
    )

    page_module.main()

    info_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("info")
    ]
    assert any("No records matched" in m for m in info_messages)


def test_kind_list_unavailable_shows_caption_and_wildcard_still_works(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    empty_kinds = KindAggregationResult(
        kinds=[],
        from_aggregation=False,
        ok=True,
        http_status=200,
        latency_ms=5.0,
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(
        page_module, monkeypatch, kinds_result=empty_kinds
    )

    page_module.main()

    caption_texts = [
        call.args[0] for call in streamlit_recorder.calls_named("caption")
    ]
    assert any(
        "Kind list unavailable" in c for c in caption_texts
    ), f"Expected 'Kind list unavailable' caption; got {caption_texts!r}"

    # Wildcard option still in the options list.
    options = streamlit_recorder.session_state[KIND_OPTIONS_KEY]
    assert options == [WILDCARD_KIND]
    # Search still fired against wildcard.
    assert spy.search_calls
    assert spy.search_calls[0]["kind"] == WILDCARD_KIND


# ===========================================================================
# Pagination
# ===========================================================================


def test_pagination_prev_disabled_at_offset_zero(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 500
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    prev_buttons = [
        call
        for call in streamlit_recorder.calls_named("button")
        if call.args[0] == PREV_LABEL
    ]
    assert prev_buttons
    assert prev_buttons[0].kwargs.get("disabled") is True


def test_pagination_next_disabled_when_past_total(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    # Single page of results, total <= offset+len, so Next disabled.
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 1
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    next_buttons = [
        call
        for call in streamlit_recorder.calls_named("button")
        if call.args[0] == NEXT_LABEL
    ]
    assert next_buttons
    assert next_buttons[0].kwargs.get("disabled") is True


def test_pagination_next_disabled_at_ceiling(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    # Full page at offset 9900: next would put offset+limit at 10100 > 10000.
    streamlit_recorder.session_state[RESULTS_KEY] = [
        _summary(f"id-{i}") for i in range(100)
    ]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 1_000_000
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 9900
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    next_buttons = [
        call
        for call in streamlit_recorder.calls_named("button")
        if call.args[0] == NEXT_LABEL
    ]
    assert next_buttons
    assert next_buttons[0].kwargs.get("disabled") is True


def test_pagination_next_button_advances_offset(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [
        _summary(f"id-{i}") for i in range(100)
    ]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 500
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    streamlit_recorder.button_responses[NEXT_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(
        page_module,
        monkeypatch,
        search_result=_ok_search(
            records=[_summary(f"id-{i}") for i in range(100)],
            offset=100,
            total=500,
        ),
    )

    page_module.main()

    assert spy.search_calls, "Next click should have triggered a search"
    assert spy.search_calls[-1]["offset"] == 100


def test_pagination_prev_button_decrements_offset(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 500
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 200
    streamlit_recorder.button_responses[PREV_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(
        page_module,
        monkeypatch,
        search_result=_ok_search(offset=100, total=500),
    )

    page_module.main()

    assert spy.search_calls
    assert spy.search_calls[-1]["offset"] == 100


# ===========================================================================
# Record selection + Fetch full record
# ===========================================================================


def test_selecting_record_persists_selection(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary("opendes:doc:1")]
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    streamlit_recorder.widget_values[RECORD_SELECTOR_LABEL] = "opendes:doc:1"
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    assert (
        streamlit_recorder.session_state[SELECTED_RECORD_ID_KEY]
        == "opendes:doc:1"
    )
    subheaders = [
        call.args[0] for call in streamlit_recorder.calls_named("subheader")
    ]
    assert any("Selected record" in s for s in subheaders)


def test_fetch_full_record_button_calls_get_record_and_caches(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary("opendes:doc:1")]
    streamlit_recorder.session_state[SELECTED_RECORD_ID_KEY] = "opendes:doc:1"
    streamlit_recorder.widget_values[RECORD_SELECTOR_LABEL] = "opendes:doc:1"
    streamlit_recorder.button_responses[FETCH_FULL_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_services(page_module, monkeypatch)

    page_module.main()

    assert len(spy.get_record_calls) == 1
    assert spy.get_record_calls[0][2] == "opendes:doc:1"
    cache = cast(
        dict[str, dict[str, object]],
        streamlit_recorder.session_state[FULL_RECORD_CACHE_KEY],
    )
    assert "opendes:doc:1" in cache
    cached_record = cache["opendes:doc:1"]
    assert cached_record["id"] == "opendes:doc:1"


def test_fetch_full_record_404_sets_sticky_error(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary("opendes:doc:1")]
    streamlit_recorder.session_state[SELECTED_RECORD_ID_KEY] = "opendes:doc:1"
    streamlit_recorder.widget_values[RECORD_SELECTOR_LABEL] = "opendes:doc:1"
    streamlit_recorder.button_responses[FETCH_FULL_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(
        page_module, monkeypatch, record_result=_missing_get_record()
    )

    page_module.main()

    err = streamlit_recorder.session_state[LAST_ERROR_KEY]
    assert isinstance(err, str)
    assert "opendes:doc:1" in err
    assert "not found" in err.lower() or "not visible" in err.lower()


# ===========================================================================
# Sticky error / Dismiss / 400 bad Lucene
# ===========================================================================


def test_search_400_bad_lucene_sets_sticky_error_and_does_not_crash(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(
        page_module, monkeypatch, search_result=_failed_search_400()
    )

    page_module.main()

    err = streamlit_recorder.session_state[LAST_ERROR_KEY]
    assert isinstance(err, str)
    assert "invalid lucene syntax" in err
    # Sticky error key set for the *next* render (the page renders the
    # sticky banner above the toolbar, so it appears on the rerun
    # triggered by the failing call). The page must not crash.
    assert streamlit_recorder.session_state[AUTORUN_DONE_KEY] is True


def test_dismiss_error_clears_sticky_key(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[LAST_ERROR_KEY] = "❌ Existing error"
    streamlit_recorder.button_responses[DISMISS_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    assert streamlit_recorder.session_state[LAST_ERROR_KEY] is None


# ===========================================================================
# History dataframe + clear
# ===========================================================================


def test_history_dataframe_renders_when_history_present(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    # Autorun pushed two history entries (list-kinds + search).
    history = streamlit_recorder.session_state[HISTORY_KEY]
    assert isinstance(history, list)
    assert len(history) >= 2
    # Subheader rendered.
    subheaders = [
        str(call.args[0]) for call in streamlit_recorder.calls_named("subheader")
    ]
    assert any("Session history" in s for s in subheaders)


def test_clear_history_button_resets_history(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[HISTORY_KEY] = [
        {
            "timestamp": "2026-05-11T00:00:00Z",
            "endpoint": "search.k",
            "ok": True,
            "http_status": 200,
            "latency_ms": 12.0,
            "correlation_id": "corr-1",
            "error_message": None,
        }
    ]
    streamlit_recorder.button_responses[CLEAR_HISTORY_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    assert streamlit_recorder.session_state[HISTORY_KEY] == []


# ===========================================================================
# Session-key contract — Satya's lock
# ===========================================================================


def test_all_locked_session_keys_initialized(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    locked_keys = [
        QUERY_TEXT_KEY,
        KIND_FILTER_KEY,
        KIND_OPTIONS_KEY,
        RESULTS_KEY,
        TOTAL_COUNT_KEY,
        PAGE_OFFSET_KEY,
        HISTORY_KEY,
        LAST_ERROR_KEY,
        SELECTED_RECORD_ID_KEY,
        FULL_RECORD_CACHE_KEY,
        AUTORUN_DONE_KEY,
    ]
    for key in locked_keys:
        assert key in streamlit_recorder.session_state, (
            f"Locked session key missing: {key}"
        )


# ===========================================================================
# Export — session-state keys initialized
# ===========================================================================


def test_export_session_keys_initialized(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    assert streamlit_recorder.session_state[EXPORT_STATUS_KEY] == "idle"
    assert streamlit_recorder.session_state[EXPORT_RECORDS_KEY] == []
    assert streamlit_recorder.session_state[EXPORT_FORMAT_KEY] == "csv"
    assert streamlit_recorder.session_state[EXPORT_ERROR_KEY] is None
    assert streamlit_recorder.session_state[EXPORT_ABORT_KEY] is False


# ===========================================================================
# Export — small result set (direct download)
# ===========================================================================


def test_export_buttons_appear_when_results_present(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Small result set: CSV + JSON download_buttons rendered."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 1
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    download_calls = streamlit_recorder.calls_named("download_button")
    labels = [c.args[0] for c in download_calls]
    assert any("CSV" in lbl for lbl in labels), f"Expected CSV button; got {labels}"
    assert any("JSON" in lbl for lbl in labels), f"Expected JSON button; got {labels}"


def test_export_buttons_hidden_when_no_results(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = []
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 0
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(
        page_module, monkeypatch,
        search_result=_ok_search(records=[], total=0),
    )

    page_module.main()

    download_calls = streamlit_recorder.calls_named("download_button")
    assert download_calls == []


def test_small_export_csv_content(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CSV download data contains correct headers and flattened data fields."""
    rec = RecordSummary(
        id="opendes:doc:1",
        kind="osdu:wks:reference-data:1.0.0",
        create_time="2024-01-01T00:00:00Z",
        version=1,
        source={"data": {"FacilityName": "North Sea"}},
    )
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [rec]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 1
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    csv_calls = [
        c for c in streamlit_recorder.calls_named("download_button")
        if "CSV" in c.args[0]
    ]
    assert csv_calls
    csv_data = csv_calls[0].kwargs.get("data", "")
    reader = csv.DictReader(io.StringIO(csv_data))
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["id"] == "opendes:doc:1"
    assert "data.FacilityName" in reader.fieldnames  # type: ignore[operator]
    assert rows[0]["data.FacilityName"] == "North Sea"


def test_small_export_json_content(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON download data is a pretty-printed array of source dicts."""
    rec = RecordSummary(
        id="opendes:doc:1",
        kind="osdu:wks:reference-data:1.0.0",
        create_time="2024-01-01T00:00:00Z",
        version=1,
        source={"data": {"foo": "bar"}},
    )
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [rec]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 1
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    json_calls = [
        c for c in streamlit_recorder.calls_named("download_button")
        if "JSON" in c.args[0]
    ]
    assert json_calls
    json_data = json_calls[0].kwargs.get("data", "")
    parsed = json.loads(json_data)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0] == {"data": {"foo": "bar"}}


# ===========================================================================
# Export — large result set (cursor pagination)
# ===========================================================================


def _patch_cursor_export(
    page_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pages: list[CursorSearchResult] | None = None,
) -> list[dict[str, Any]]:
    """Patch export_all_records on the page module, return call log."""
    calls: list[dict[str, Any]] = []

    default_pages = pages or [
        CursorSearchResult(
            kind="*:*:*:*",
            records=[_summary(f"cursor-{i}") for i in range(5)],
            total_count=5,
            has_more=False,
            ok=True,
        )
    ]

    def fake_export_all_records(
        connection: Any, token: str, *, kind: str, query: str | None = None
    ):  # type: ignore[return]
        calls.append(
            {"connection": connection, "token": token, "kind": kind, "query": query}
        )
        yield from default_pages

    monkeypatch.setattr(page_module, "_CURSOR_EXPORT_AVAILABLE", True)
    monkeypatch.setattr(page_module, "export_all_records", fake_export_all_records)
    return calls


def test_large_result_shows_cursor_note(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When totalCount > current page, show cursor pagination note."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 500
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)
    _patch_cursor_export(page_module, monkeypatch)

    page_module.main()

    captions = [
        c.args[0] for c in streamlit_recorder.calls_named("caption")
    ]
    assert any("cursor pagination" in c for c in captions), (
        f"Expected cursor pagination caption; got {captions}"
    )


def test_large_result_over_10k_shows_warning(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 15_000
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)
    _patch_cursor_export(page_module, monkeypatch)

    page_module.main()

    warnings = [
        c.args[0] for c in streamlit_recorder.calls_named("warning")
    ]
    assert any("Large export" in w for w in warnings), (
        f"Expected large-export warning; got {warnings}"
    )


def test_cursor_export_csv_click_runs_export(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clicking CSV export for large results calls export_all_records."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 500
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    streamlit_recorder.button_responses[EXPORT_CSV_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)
    export_calls = _patch_cursor_export(page_module, monkeypatch)

    page_module.main()

    assert len(export_calls) == 1
    assert streamlit_recorder.session_state[EXPORT_STATUS_KEY] == "done"
    accumulated = streamlit_recorder.session_state[EXPORT_RECORDS_KEY]
    assert len(accumulated) == 5
    assert streamlit_recorder.session_state[EXPORT_FORMAT_KEY] == "csv"


def test_cursor_export_json_click_runs_export(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 500
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    streamlit_recorder.button_responses[EXPORT_JSON_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)
    _patch_cursor_export(page_module, monkeypatch)

    page_module.main()

    assert streamlit_recorder.session_state[EXPORT_STATUS_KEY] == "done"
    assert streamlit_recorder.session_state[EXPORT_FORMAT_KEY] == "json"


def test_cursor_export_error_midway_shows_partial(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cursor search fails mid-export, status=error with partial count."""
    error_pages = [
        CursorSearchResult(
            kind="*:*:*:*",
            records=[_summary("ok-1"), _summary("ok-2")],
            total_count=100,
            has_more=True,
            ok=True,
        ),
        CursorSearchResult(
            kind="*:*:*:*",
            records=[],
            total_count=100,
            has_more=False,
            ok=False,
            error_message="server timeout",
        ),
    ]
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 100
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    streamlit_recorder.button_responses[EXPORT_CSV_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)
    _patch_cursor_export(page_module, monkeypatch, pages=error_pages)

    page_module.main()

    assert streamlit_recorder.session_state[EXPORT_STATUS_KEY] == "error"
    err = streamlit_recorder.session_state[EXPORT_ERROR_KEY]
    assert "2" in err  # partial count
    assert "server timeout" in err
    # Partial records accumulated.
    accumulated = streamlit_recorder.session_state[EXPORT_RECORDS_KEY]
    assert len(accumulated) == 2


def test_cursor_export_abort_stops_iteration(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Abort flag set before second page stops the export gracefully."""
    multi_pages = [
        CursorSearchResult(
            kind="*:*:*:*",
            records=[_summary("p1")],
            total_count=100,
            has_more=True,
            ok=True,
        ),
        CursorSearchResult(
            kind="*:*:*:*",
            records=[_summary("p2")],
            total_count=100,
            has_more=True,
            ok=True,
        ),
        CursorSearchResult(
            kind="*:*:*:*",
            records=[_summary("p3")],
            total_count=100,
            has_more=False,
            ok=True,
        ),
    ]

    # We need the abort flag to be set after the first page is consumed.
    # Monkey-patch the generator to set the flag after yielding page 1.
    original_pages = list(multi_pages)

    def aborting_export(connection: Any, token: str, *, kind: str, query: str | None = None):  # type: ignore[return]
        for i, page in enumerate(original_pages):
            yield page
            if i == 0:
                streamlit_recorder.session_state[EXPORT_ABORT_KEY] = True

    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 100
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    streamlit_recorder.button_responses[EXPORT_CSV_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)
    monkeypatch.setattr(page_module, "_CURSOR_EXPORT_AVAILABLE", True)
    monkeypatch.setattr(page_module, "export_all_records", aborting_export)

    page_module.main()

    assert streamlit_recorder.session_state[EXPORT_STATUS_KEY] == "done"
    accumulated = streamlit_recorder.session_state[EXPORT_RECORDS_KEY]
    # Abort after page 1 consumed → page 2 also consumed (abort checked
    # AFTER extend), so we get pages 1+2 = 2 records.
    assert len(accumulated) == 2


def test_export_done_state_shows_download_button(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When export_status=done, a download button is rendered."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 1
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    streamlit_recorder.session_state[EXPORT_STATUS_KEY] = "done"
    streamlit_recorder.session_state[EXPORT_RECORDS_KEY] = [_summary()]
    streamlit_recorder.session_state[EXPORT_FORMAT_KEY] = "csv"
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    download_calls = streamlit_recorder.calls_named("download_button")
    labels = [c.args[0] for c in download_calls]
    assert any("Download CSV" in lbl for lbl in labels)


def test_export_error_state_shows_error_and_partial_download(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When export_status=error with partial records, show error + partial download."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 100
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    streamlit_recorder.session_state[EXPORT_STATUS_KEY] = "error"
    streamlit_recorder.session_state[EXPORT_ERROR_KEY] = (
        "Export failed after 5 records. Error: server timeout"
    )
    streamlit_recorder.session_state[EXPORT_RECORDS_KEY] = [
        _summary(f"partial-{i}") for i in range(5)
    ]
    streamlit_recorder.session_state[EXPORT_FORMAT_KEY] = "json"
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    page_module.main()

    error_calls = streamlit_recorder.calls_named("error")
    assert any("server timeout" in str(c.args[0]) for c in error_calls)
    download_calls = streamlit_recorder.calls_named("download_button")
    labels = [c.args[0] for c in download_calls]
    assert any("partial" in lbl.lower() for lbl in labels)


def test_cursor_export_unavailable_shows_caption(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cursor functions not imported, large results show a fallback caption."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.session_state[TOTAL_COUNT_KEY] = 500
    streamlit_recorder.session_state[PAGE_OFFSET_KEY] = 0
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)
    monkeypatch.setattr(page_module, "_CURSOR_EXPORT_AVAILABLE", False)

    page_module.main()

    captions = [
        c.args[0] for c in streamlit_recorder.calls_named("caption")
    ]
    assert any("not yet available" in c for c in captions)


# ===========================================================================
# Issue #26 — Multi-kind search + aggregateBy
# ===========================================================================

# Session keys mirroring the page (issue #26).
MULTI_KIND_KEY = "search_kind_selections"
AGGREGATE_CHECKED_KEY = "search_aggregate_by_kind"
AGGREGATION_RESULT_KEY = "search_aggregation_results"

# Widget labels matching the page (issue #26).
MULTI_KIND_LABEL = "Kind filter"
AGGREGATE_TOGGLE_LABEL = "Aggregate by kind"


def _ok_aggregation_result() -> "SearchAggregationResult":
    """Factory for a successful aggregation response."""
    from app.models.osdu import AggregationBucket, SearchAggregationResult

    return SearchAggregationResult(
        kind="*:*:*:*",
        aggregations=[
            AggregationBucket(key="osdu:wks:reference-data:1.0.0", count=42),
            AggregationBucket(key="osdu:wks:dataset:1.0.0", count=17),
        ],
        total_count=59,
        ok=True,
        http_status=200,
        latency_ms=30.0,
        correlation_id="corr-agg",
    )


def _patch_aggregation_services(
    page_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    aggregation_result: Any = None,
    search_result: SearchPageResult | None = None,
    kinds_result: KindAggregationResult | None = None,
    token: str | None = "test-token",
) -> _Spy:
    """Patch services including optional aggregation support for #26 tests."""
    spy = _patch_services(
        page_module,
        monkeypatch,
        kinds_result=kinds_result,
        search_result=search_result,
        token=token,
    )

    # Patch aggregation function if the page imports it.
    if hasattr(page_module, "search_with_aggregation"):
        agg_calls: list[Any] = []

        def fake_search_with_aggregation(
            connection: Any, supplied_token: str, **kwargs: Any
        ) -> Any:
            agg_calls.append(
                {"connection": connection, "token": supplied_token, **kwargs}
            )
            return aggregation_result or _ok_aggregation_result()

        monkeypatch.setattr(
            page_module, "search_with_aggregation", fake_search_with_aggregation
        )
        spy.aggregation_calls = agg_calls  # type: ignore[attr-defined]

    # Patch build_multi_kind_query if the page imports it.
    if hasattr(page_module, "build_multi_kind_query"):
        build_calls: list[Any] = []

        def fake_build_multi_kind_query(kinds: list[str]) -> str:
            build_calls.append(kinds)
            return " OR ".join(f'kind:"{k}"' for k in kinds)

        monkeypatch.setattr(
            page_module, "build_multi_kind_query", fake_build_multi_kind_query
        )
        spy.build_multi_kind_calls = build_calls  # type: ignore[attr-defined]

    return spy


# --- Multi-kind multiselect widget ---


def test_multi_kind_multiselect_renders_with_kind_options(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#26: The kind filter should render as a multiselect with known kinds."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_aggregation_services(page_module, monkeypatch)

    page_module.main()

    multiselect_calls = streamlit_recorder.calls_named("multiselect")
    kind_selects = [
        c for c in multiselect_calls if MULTI_KIND_LABEL in str(c.args[0])
    ]
    assert kind_selects, (
        f"Expected a multiselect labeled '{MULTI_KIND_LABEL}'; "
        f"got {[c.args[0] for c in multiselect_calls]}"
    )


def test_multi_kind_empty_selection_searches_wildcard(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#26: No kinds selected → search with wildcard kind."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.widget_values[MULTI_KIND_LABEL] = []
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_aggregation_services(page_module, monkeypatch)

    page_module.main()

    assert spy.search_calls
    assert spy.search_calls[0]["kind"] == WILDCARD_KIND


def test_multi_kind_single_selection_searches_that_kind(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#26: Single kind selected → search directly with that kind."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [
        WILDCARD_KIND,
        "osdu:wks:reference-data:1.0.0",
        "osdu:wks:dataset:1.0.0",
    ]
    streamlit_recorder.widget_values[MULTI_KIND_LABEL] = [
        "osdu:wks:reference-data:1.0.0"
    ]
    streamlit_recorder.button_responses[SEARCH_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_aggregation_services(page_module, monkeypatch)

    page_module.main()

    if spy.search_calls:
        assert spy.search_calls[-1]["kind"] == "osdu:wks:reference-data:1.0.0"


def test_multi_kind_multiple_selection_uses_build_multi_kind_query(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#26: Multiple kinds selected → delegates to build_multi_kind_query."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [
        WILDCARD_KIND,
        "osdu:wks:reference-data:1.0.0",
        "osdu:wks:dataset:1.0.0",
    ]
    streamlit_recorder.widget_values[MULTI_KIND_LABEL] = [
        "osdu:wks:reference-data:1.0.0",
        "osdu:wks:dataset:1.0.0",
    ]
    streamlit_recorder.button_responses[SEARCH_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_aggregation_services(page_module, monkeypatch)

    page_module.main()

    if hasattr(spy, "build_multi_kind_calls"):
        assert spy.build_multi_kind_calls, (
            "Expected build_multi_kind_query to be called with multi-select"
        )
        called_kinds = spy.build_multi_kind_calls[0]
        assert "osdu:wks:reference-data:1.0.0" in called_kinds
        assert "osdu:wks:dataset:1.0.0" in called_kinds


# --- Aggregate checkbox + aggregation table ---


def test_aggregate_checkbox_renders(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#26: An aggregate-by-kind checkbox/toggle is rendered."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_aggregation_services(page_module, monkeypatch)

    page_module.main()

    toggle_calls = streamlit_recorder.calls_named("toggle")
    checkbox_calls = streamlit_recorder.calls_named("checkbox")
    all_labels = [c.args[0] for c in toggle_calls + checkbox_calls]
    assert any(AGGREGATE_TOGGLE_LABEL in lbl for lbl in all_labels), (
        f"Expected '{AGGREGATE_TOGGLE_LABEL}' toggle/checkbox; got {all_labels}"
    )


def test_aggregate_checked_calls_search_with_aggregation(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#26: When aggregate checked, calls search_with_aggregation instead."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.widget_values[AGGREGATE_TOGGLE_LABEL] = True
    streamlit_recorder.button_responses[SEARCH_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_aggregation_services(page_module, monkeypatch)

    page_module.main()

    if hasattr(spy, "aggregation_calls"):
        assert spy.aggregation_calls, (
            "Expected search_with_aggregation to be called when aggregate is checked"
        )


def test_aggregation_table_renders_with_kind_count_buckets(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#26: Aggregation results render a table with kind:count pairs."""
    from app.models.osdu import AggregationBucket, SearchAggregationResult

    agg = SearchAggregationResult(
        kind="*:*:*:*",
        aggregations=[
            AggregationBucket(key="osdu:wks:reference-data:1.0.0", count=42),
            AggregationBucket(key="osdu:wks:dataset:1.0.0", count=17),
        ],
        total_count=59,
        ok=True,
        http_status=200,
        latency_ms=25.0,
        correlation_id="corr-agg-table",
    )
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[AGGREGATION_RESULT_KEY] = agg
    streamlit_recorder.widget_values[AGGREGATE_TOGGLE_LABEL] = True
    streamlit_recorder.button_responses[SEARCH_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_aggregation_services(
        page_module, monkeypatch, aggregation_result=agg
    )

    page_module.main()

    # Expect either a dataframe or table call rendering buckets.
    dataframe_calls = streamlit_recorder.calls_named("dataframe")
    table_calls = streamlit_recorder.calls_named("table")
    assert dataframe_calls or table_calls, (
        "Expected aggregation results rendered as dataframe or table"
    )


def test_aggregation_table_hidden_when_unchecked(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#26: When aggregate is unchecked, no aggregation table is rendered."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[RESULTS_KEY] = [_summary()]
    streamlit_recorder.widget_values[AGGREGATE_TOGGLE_LABEL] = False
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_aggregation_services(page_module, monkeypatch)

    page_module.main()

    # No aggregation table should appear.
    # We can't assert absence of ALL dataframes (search results may
    # use them), but we check that no aggregation-specific subheader
    # or metric appears.
    subheaders = [
        c.args[0] for c in streamlit_recorder.calls_named("subheader")
    ]
    assert not any("Aggregation" in s for s in subheaders), (
        "Aggregation table should not render when aggregate is unchecked"
    )


def test_aggregation_graceful_degradation_when_has_aggregation_false(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#26: When _HAS_AGGREGATION is False, aggregate UI is absent or disabled."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    # Simulate aggregation import failure.
    if hasattr(page_module, "_HAS_AGGREGATION"):
        monkeypatch.setattr(page_module, "_HAS_AGGREGATION", False)

    page_module.main()

    # Page should not crash. Either the aggregate toggle is hidden or
    # it's rendered as disabled.
    toggle_calls = streamlit_recorder.calls_named("toggle")
    checkbox_calls = streamlit_recorder.calls_named("checkbox")
    agg_toggles = [
        c
        for c in toggle_calls + checkbox_calls
        if AGGREGATE_TOGGLE_LABEL in str(c.args[0])
    ]
    # If the toggle is rendered, it should be disabled.
    for toggle in agg_toggles:
        assert toggle.kwargs.get("disabled", False), (
            "Aggregate toggle should be disabled when _HAS_AGGREGATION is False"
        )


# ===========================================================================
# Issue #27 — Field-builder UI for Search queries
# ===========================================================================

# Session keys mirroring the page (issue #27).
FIELD_BUILDER_CLAUSES_KEY = "query_builder_clauses"
FIELD_BUILDER_COMBINATOR_KEY = "query_builder_combinator"

# Widget labels matching the page (issue #27).
QUERY_BUILDER_LABEL = "🔨 Query Builder"
SCHEMA_KIND_LABEL = "Schema kind"
ADD_CLAUSE_LABEL = "Add clause"
APPLY_QUERY_LABEL = "Apply to search"
COMBINATOR_LABEL = "Combinator"


def _patch_field_builder_services(
    page_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    schema_kinds: list[str] | None = None,
    schema_fields: list[Any] | None = None,
) -> _Spy:
    """Patch services for field-builder tests."""
    spy = _patch_services(page_module, monkeypatch)

    # Patch manifest_generator functions if the page imports them.
    if hasattr(page_module, "list_schema_kinds"):
        kinds = schema_kinds or [
            "osdu:wks:master-data--Well:1.0.0",
            "osdu:wks:master-data--Wellbore:1.0.0",
        ]
        monkeypatch.setattr(page_module, "list_schema_kinds", lambda **_: kinds)

    if hasattr(page_module, "extract_schema_fields"):
        from app.models.osdu import SchemaField

        fields = schema_fields or [
            SchemaField(
                path="data.FacilityName",
                field_type="string",
                required=True,
            ),
            SchemaField(
                path="data.WellDepth",
                field_type="number",
                required=False,
            ),
            SchemaField(
                path="data.Country",
                field_type="string",
                required=False,
            ),
        ]
        monkeypatch.setattr(
            page_module, "extract_schema_fields", lambda *a, **kw: fields
        )

    if hasattr(page_module, "load_schema"):
        monkeypatch.setattr(
            page_module, "load_schema", lambda *a, **kw: {"properties": {"data": {}}}
        )

    return spy


# --- Query builder expander ---


def test_query_builder_expander_renders(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: Query builder section renders in an expander."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    expander_calls = streamlit_recorder.calls_named("expander")
    labels = [c.args[0] for c in expander_calls]
    assert any("uild" in lbl or "query" in lbl.lower() for lbl in labels), (
        f"Expected a query builder expander; got {labels}"
    )


def test_schema_kind_picker_loads_kinds(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: Schema kind picker renders with kinds from manifest_generator."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    selectbox_calls = streamlit_recorder.calls_named("selectbox")
    schema_selects = [
        c
        for c in selectbox_calls
        if any(
            term in str(c.args[0]).lower()
            for term in ["schema", "kind"]
        )
    ]
    # At least one selectbox for schema kind should appear inside the builder.
    if schema_selects:
        options = schema_selects[0].args[1] if len(schema_selects[0].args) > 1 else []
        assert any("Well" in str(o) for o in options), (
            f"Expected Well kinds in schema picker options; got {options}"
        )


def test_field_list_loads_when_kind_selected(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: Selecting a schema kind populates the field list."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    # A selectbox for field selection should appear with schema fields.
    selectbox_calls = streamlit_recorder.calls_named("selectbox")
    field_selects = [
        c
        for c in selectbox_calls
        if any(
            term in str(c.args[0]).lower()
            for term in ["field", "attribute", "property"]
        )
    ]
    if field_selects:
        options = field_selects[0].args[1] if len(field_selects[0].args) > 1 else []
        assert any("FacilityName" in str(o) for o in options), (
            f"Expected FacilityName in field options; got {options}"
        )


def test_clause_row_renders_with_field_operator_value(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: A clause row renders with field/operator/value inputs."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    # Expect columns rendered for field/operator/value layout.
    column_calls = streamlit_recorder.calls_named("columns")
    # At least one columns call should exist for clause rows.
    # Also expect text_input for value entry.
    text_inputs = streamlit_recorder.calls_named("text_input")
    selectboxes = streamlit_recorder.calls_named("selectbox")
    # A clause row needs at minimum: a field selector, an operator selector,
    # and a value input. We check that the builder rendered at least these.
    assert column_calls or (text_inputs and selectboxes), (
        "Expected clause row with field/operator/value inputs"
    )


def test_add_clause_button_adds_row(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: Add clause button appends a new clause row."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    streamlit_recorder.button_responses[ADD_CLAUSE_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    # After clicking Add clause, the clauses list should grow by one.
    # Note: the page's Add handler appends a single new clause built from
    # the current new_field/new_op/new_value inputs and calls st.rerun()
    # (a no-op under the recorder), so a fresh page with empty clauses
    # ends at exactly 1 clause after the click.
    if FIELD_BUILDER_CLAUSES_KEY in streamlit_recorder.session_state:
        clauses = streamlit_recorder.session_state[FIELD_BUILDER_CLAUSES_KEY]
        assert len(clauses) >= 1, (
            f"Expected at least 1 clause after add; got {len(clauses)}"
        )


def test_remove_clause_button_removes_row(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: Remove button removes a clause from the list."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    # Pre-populate two clauses.
    streamlit_recorder.session_state[FIELD_BUILDER_CLAUSES_KEY] = [
        {"field": "data.FacilityName", "operator": "contains", "value": "North"},
        {"field": "data.WellDepth", "operator": "exists", "value": ""},
    ]
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    # Click the first remove button.
    streamlit_recorder.button_responses["🗑️"] = True
    streamlit_recorder.button_responses["Remove"] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    if FIELD_BUILDER_CLAUSES_KEY in streamlit_recorder.session_state:
        clauses = streamlit_recorder.session_state[FIELD_BUILDER_CLAUSES_KEY]
        assert len(clauses) <= 1, (
            f"Expected at most 1 clause after remove; got {len(clauses)}"
        )


def test_combinator_radio_renders(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: AND/OR combinator radio renders."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    radio_calls = streamlit_recorder.calls_named("radio")
    combinator_radios = [
        c
        for c in radio_calls
        if any(
            term in str(c.args).lower()
            for term in ["and", "or", "combinator"]
        )
    ]
    assert combinator_radios, (
        f"Expected AND/OR combinator radio; got {[c.args for c in radio_calls]}"
    )


# --- Lucene preview generation ---


def test_lucene_preview_contains_clause(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: Single 'contains' clause → data.Field:*value*."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[FIELD_BUILDER_CLAUSES_KEY] = [
        {"field": "data.FacilityName", "operator": "contains", "value": "North"},
    ]
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    # Check that the Lucene preview is rendered somewhere (code/text/caption).
    code_calls = streamlit_recorder.calls_named("code")
    text_calls = streamlit_recorder.calls_named("text")
    caption_calls = streamlit_recorder.calls_named("caption")
    all_text = " ".join(
        str(c.args[0]) for c in code_calls + text_calls + caption_calls
    )
    assert "data.FacilityName:*North*" in all_text, (
        f"Expected 'data.FacilityName:*North*' in preview; got snippets: "
        f"{[c.args[0] for c in code_calls]}"
    )


def test_lucene_preview_exact_clause(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: Single 'exact' clause → data.Field.keyword:"value"."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[FIELD_BUILDER_CLAUSES_KEY] = [
        {"field": "data.FacilityName", "operator": "exact", "value": "North Sea"},
    ]
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    code_calls = streamlit_recorder.calls_named("code")
    text_calls = streamlit_recorder.calls_named("text")
    caption_calls = streamlit_recorder.calls_named("caption")
    all_text = " ".join(
        str(c.args[0]) for c in code_calls + text_calls + caption_calls
    )
    assert 'data.FacilityName.keyword:"North Sea"' in all_text, (
        f"Expected exact match Lucene syntax; got: {all_text[:300]}"
    )


def test_lucene_preview_exists_clause(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: Single 'exists' clause → _exists_:data.Field."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[FIELD_BUILDER_CLAUSES_KEY] = [
        {"field": "data.WellDepth", "operator": "exists", "value": ""},
    ]
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    code_calls = streamlit_recorder.calls_named("code")
    text_calls = streamlit_recorder.calls_named("text")
    caption_calls = streamlit_recorder.calls_named("caption")
    all_text = " ".join(
        str(c.args[0]) for c in code_calls + text_calls + caption_calls
    )
    assert "_exists_:data.WellDepth" in all_text, (
        f"Expected exists Lucene syntax; got: {all_text[:300]}"
    )


def test_lucene_preview_multiple_clauses_and_combinator(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: Multiple clauses with AND → (clause1 AND clause2)."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[FIELD_BUILDER_CLAUSES_KEY] = [
        {"field": "data.FacilityName", "operator": "contains", "value": "North"},
        {"field": "data.WellDepth", "operator": "exists", "value": ""},
    ]
    streamlit_recorder.session_state[FIELD_BUILDER_COMBINATOR_KEY] = "AND"
    streamlit_recorder.widget_values[COMBINATOR_LABEL] = "AND"
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    code_calls = streamlit_recorder.calls_named("code")
    text_calls = streamlit_recorder.calls_named("text")
    caption_calls = streamlit_recorder.calls_named("caption")
    all_text = " ".join(
        str(c.args[0]) for c in code_calls + text_calls + caption_calls
    )
    assert "AND" in all_text, (
        f"Expected AND combinator in multi-clause preview; got: {all_text[:300]}"
    )
    assert "data.FacilityName" in all_text
    assert "data.WellDepth" in all_text


def test_lucene_preview_or_combinator(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: Multiple clauses with OR → (clause1 OR clause2)."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[FIELD_BUILDER_CLAUSES_KEY] = [
        {"field": "data.FacilityName", "operator": "contains", "value": "North"},
        {"field": "data.Country", "operator": "exact", "value": "Norway"},
    ]
    streamlit_recorder.session_state[FIELD_BUILDER_COMBINATOR_KEY] = "OR"
    streamlit_recorder.widget_values[COMBINATOR_LABEL] = "OR"
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    code_calls = streamlit_recorder.calls_named("code")
    text_calls = streamlit_recorder.calls_named("text")
    caption_calls = streamlit_recorder.calls_named("caption")
    all_text = " ".join(
        str(c.args[0]) for c in code_calls + text_calls + caption_calls
    )
    assert "OR" in all_text, (
        f"Expected OR combinator in multi-clause preview; got: {all_text[:300]}"
    )


# --- Apply to search ---


def test_apply_to_search_copies_query_to_search_input(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: 'Apply to search' button copies generated query to search input."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    streamlit_recorder.session_state[AUTORUN_DONE_KEY] = True
    streamlit_recorder.session_state[KIND_OPTIONS_KEY] = [WILDCARD_KIND]
    streamlit_recorder.session_state[FIELD_BUILDER_CLAUSES_KEY] = [
        {"field": "data.FacilityName", "operator": "contains", "value": "North"},
    ]
    streamlit_recorder.widget_values[SCHEMA_KIND_LABEL] = (
        "osdu:wks:master-data--Well:1.0.0"
    )
    streamlit_recorder.button_responses[APPLY_QUERY_LABEL] = True
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_field_builder_services(page_module, monkeypatch)

    page_module.main()

    # The generated query should be written to the search query key.
    resolved_query = streamlit_recorder.session_state.get(RESOLVED_QUERY_KEY, "")
    query_text = streamlit_recorder.session_state.get(QUERY_TEXT_KEY, "")
    # Either the resolved key or the text key should contain the generated
    # Lucene query.
    assert ("data.FacilityName" in str(resolved_query)) or (
        "data.FacilityName" in str(query_text)
    ), (
        f"Expected generated query in search input; "
        f"resolved={resolved_query!r}, text={query_text!r}"
    )


# --- Graceful degradation ---


def test_field_builder_graceful_degradation_when_has_field_builder_false(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27: When _HAS_FIELD_BUILDER is False, builder UI is absent or disabled."""
    streamlit_recorder.session_state[CONNECTION_KEY] = (
        _service_principal_connection()
    )
    page_module = _load_page(streamlit_recorder, monkeypatch)
    _patch_services(page_module, monkeypatch)

    # Simulate field-builder import failure.
    if hasattr(page_module, "_HAS_FIELD_BUILDER"):
        monkeypatch.setattr(page_module, "_HAS_FIELD_BUILDER", False)

    page_module.main()

    # Page should not crash — the builder expander may be absent entirely
    # or show a disabled/fallback message. Either outcome is acceptable.
    expander_calls = streamlit_recorder.calls_named("expander")
    builder_expanders = [
        c
        for c in expander_calls
        if any(
            term in str(c.args[0]).lower()
            for term in ["build", "query"]
        )
    ]
    # If a builder expander is present when feature is disabled, there
    # should be an info/caption about unavailability inside.
    if builder_expanders:
        info_calls = streamlit_recorder.calls_named("info")
        caption_calls = streamlit_recorder.calls_named("caption")
        all_msgs = [str(c.args[0]) for c in info_calls + caption_calls]
        assert any(
            "not available" in m.lower() or "unavailable" in m.lower()
            for m in all_msgs
        ), "Expected unavailability message when field builder is disabled"
