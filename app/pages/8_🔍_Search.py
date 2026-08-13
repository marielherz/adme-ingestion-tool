"""Search page for ADME — Operate › Search.

Lets operators browse and query records via Search v2, paginate within
the OSDU offset+limit ≤ 10,000 ceiling, and fetch full records from
Storage v2 for inspection. Layout, session keys, and edge-case behavior
are locked by ``.squad/decisions/inbox/satya-search-page-contract.md``.

Notes:
- We never reassign a session_state key after the widget bound to that
  key has rendered in the current run. The Search button writes to a
  *separate* "resolved" key (:data:`SEARCH_RESOLVED_QUERY_KEY`) and the
  service module is called from that — never from the widget-bound
  :data:`SEARCH_QUERY_TEXT_KEY` after the text_input has rendered.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if PROJECT_ROOT not in {Path(path or ".").resolve() for path in sys.path}:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # type: ignore[import-untyped]  # noqa: E402
import streamlit as st  # type: ignore[import-not-found]  # noqa: E402

from app.connection_state import (  # noqa: E402
    ensure_session_defaults,
    get_connection,
    get_user_auth_state,
)
from app.models.connection import ADMEConnection, AuthMethod  # noqa: E402
from app.models.osdu import (  # noqa: E402
    CursorSearchResult,
    KindAggregationResult,
    RecordDetailResult,
    RecordSummary,
    SearchPageResult,
)
from app.services.auth import AuthenticationError, get_token  # noqa: E402
from app.services.search import (  # noqa: E402
    MAX_OFFSET_PLUS_LIMIT,
    WILDCARD_KIND,
    get_record,
    list_kinds,
    search_records,
)

try:
    from app.services.search import (  # noqa: E402
        export_all_records,
        search_with_cursor,
    )

    _CURSOR_EXPORT_AVAILABLE = True
except ImportError:
    _CURSOR_EXPORT_AVAILABLE = False

try:
    from app.services.search import (  # noqa: E402
        search_with_aggregation,
        build_multi_kind_query,
    )
    from app.models.osdu import (  # noqa: E402
        SearchAggregationResult,
        AggregationBucket,
    )

    _HAS_AGGREGATION = True
except ImportError:
    _HAS_AGGREGATION = False

try:
    from app.services.manifest_generator import (  # noqa: E402
        extract_schema_fields,
        list_schema_kinds,
        load_schema,
    )

    _HAS_FIELD_BUILDER = True
except ImportError:
    _HAS_FIELD_BUILDER = False

SETTINGS_PAGE_PATH = "pages/1_⚙️_Instance_Configuration.py"

# --- Locked session-state keys (Charlie tests these) ---------------------
SEARCH_QUERY_TEXT_KEY = "search_query_text"
SEARCH_KIND_FILTER_KEY = "search_kind_filter"
SEARCH_KIND_OPTIONS_KEY = "search_kind_options"
SEARCH_RESULTS_KEY = "search_results"
SEARCH_TOTAL_COUNT_KEY = "search_total_count"
SEARCH_PAGE_OFFSET_KEY = "search_page_offset"
SEARCH_HISTORY_KEY = "search_history"
SEARCH_LAST_ERROR_KEY = "search_last_error"
SEARCH_SELECTED_RECORD_ID_KEY = "search_selected_record_id"
SEARCH_FULL_RECORD_CACHE_KEY = "search_full_record_cache"
SEARCH_AUTORUN_DONE_KEY = "search_autorun_done"

# --- Internal helper keys (not part of the locked contract) ---------------
# Written by the Search button handler so we never mutate the widget-bound
# `search_query_text` key after the text_input has rendered (post-widget
# write would raise StreamlitAPIException, mirroring the 5/11 ingestion bug).
SEARCH_RESOLVED_QUERY_KEY = "search_resolved_query"

# --- Export session-state keys (issue #25) --------------------------------
EXPORT_STATUS_KEY = "export_status"  # "idle" | "exporting" | "done" | "error"
EXPORT_RECORDS_KEY = "export_records"  # list[RecordSummary] accumulated
EXPORT_FORMAT_KEY = "export_format"  # "csv" | "json"
EXPORT_ERROR_KEY = "export_error"  # str | None
EXPORT_ABORT_KEY = "export_abort_requested"  # bool — graceful mid-loop stop

# --- Multi-kind + aggregation keys (issue #26) ----------------------------
SEARCH_KIND_SELECTIONS_KEY = "search_kind_selections"
SEARCH_AGGREGATE_KEY = "search_aggregate_by_kind"
SEARCH_AGGREGATION_RESULTS_KEY = "search_aggregation_results"

# --- Query builder keys (issue #27) ---------------------------------------
QUERY_BUILDER_CLAUSES_KEY = "query_builder_clauses"
QUERY_BUILDER_COMBINATOR_KEY = "query_builder_combinator"
QUERY_BUILDER_KIND_KEY = "query_builder_kind"
QUERY_BUILDER_FIELDS_KEY = "query_builder_fields"

# --- Constants -----------------------------------------------------------
SEARCH_PAGE_SIZE = 100
SEARCH_DATA_PREVIEW_CHARS = 100
HISTORY_DISPLAY_LIMIT = 50

LABEL_LIST_KINDS = "list-kinds"
LABEL_SEARCH = "search"
LABEL_GET_RECORD = "get-record"

OSDU_SEARCH_SYNTAX_DOCS_URL = (
    "https://learn.microsoft.com/en-us/azure/energy-data-services/"
    "concepts-index-and-search"
)


def main() -> None:
    """Render the Search page."""
    st.set_page_config(
        page_title="Search · ADME Control Plane",
        page_icon="🔍",
        layout="wide",
    )
    st.title("🔍 Search records in ADME")
    st.markdown(
        "Browse and query records via the Search service, then drill into "
        "individual records from the Storage service."
    )

    ensure_session_defaults(st.session_state)
    _ensure_page_defaults()

    connection = get_connection(st.session_state)
    if not _preflight_ok(connection):
        return
    assert connection is not None  # mypy — _preflight_ok guarantees this

    st.caption(
        f"Data partition: `{connection.data_partition_id}` · "
        f"Endpoint: `{connection.endpoint}`"
    )

    _render_sticky_error()

    token = _acquire_token(connection)
    if token is None:
        return

    _autorun_once(connection, token)

    _render_toolbar(connection, token)
    _render_query_builder()
    _render_aggregation_summary()
    _render_results_section()
    _render_export_section(connection, token)
    _render_pagination(connection, token)
    _render_selected_record(connection, token)
    _render_history()


# ---------------------------------------------------------------------------
# Session bootstrap
# ---------------------------------------------------------------------------


def _ensure_page_defaults() -> None:
    """Initialize page-scoped session keys."""
    st.session_state.setdefault(SEARCH_QUERY_TEXT_KEY, "")
    st.session_state.setdefault(SEARCH_KIND_FILTER_KEY, WILDCARD_KIND)
    st.session_state.setdefault(SEARCH_KIND_OPTIONS_KEY, [])
    st.session_state.setdefault(SEARCH_RESULTS_KEY, [])
    st.session_state.setdefault(SEARCH_TOTAL_COUNT_KEY, None)
    st.session_state.setdefault(SEARCH_PAGE_OFFSET_KEY, 0)
    st.session_state.setdefault(SEARCH_HISTORY_KEY, [])
    st.session_state.setdefault(SEARCH_LAST_ERROR_KEY, None)
    st.session_state.setdefault(SEARCH_SELECTED_RECORD_ID_KEY, None)
    st.session_state.setdefault(SEARCH_FULL_RECORD_CACHE_KEY, {})
    st.session_state.setdefault(SEARCH_AUTORUN_DONE_KEY, False)
    st.session_state.setdefault(SEARCH_RESOLVED_QUERY_KEY, "")
    st.session_state.setdefault(EXPORT_STATUS_KEY, "idle")
    st.session_state.setdefault(EXPORT_RECORDS_KEY, [])
    st.session_state.setdefault(EXPORT_FORMAT_KEY, "csv")
    st.session_state.setdefault(EXPORT_ERROR_KEY, None)
    st.session_state.setdefault(EXPORT_ABORT_KEY, False)
    st.session_state.setdefault(SEARCH_KIND_SELECTIONS_KEY, [])
    st.session_state.setdefault(SEARCH_AGGREGATE_KEY, False)
    st.session_state.setdefault(SEARCH_AGGREGATION_RESULTS_KEY, [])
    st.session_state.setdefault(QUERY_BUILDER_CLAUSES_KEY, [])
    st.session_state.setdefault(QUERY_BUILDER_COMBINATOR_KEY, "AND")
    st.session_state.setdefault(QUERY_BUILDER_KIND_KEY, "")
    st.session_state.setdefault(QUERY_BUILDER_FIELDS_KEY, [])


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def _preflight_ok(connection: ADMEConnection | None) -> bool:
    """Return True when we have everything required to search."""
    if connection is None or not connection.is_valid():
        st.info(
            "No ADME connection is configured for this session. "
            "Open Instance Configuration to add your endpoint, identity "
            "details, and data partition."
        )
        st.page_link(
            SETTINGS_PAGE_PATH,
            label="Open Instance Configuration",
            icon="⚙️",
        )
        return False

    if connection.auth_method == AuthMethod.USER_IMPERSONATION:
        if get_user_auth_state(st.session_state) is None:
            st.info(
                "No token available for this session. Sign in on the "
                "Instance Configuration page to enable search."
            )
            st.page_link(
                SETTINGS_PAGE_PATH,
                label="Open Instance Configuration",
                icon="⚙️",
            )
            return False

    if not connection.data_partition_id.strip():
        st.info(
            "No data partition is configured for this connection. "
            "Open Instance Configuration to add the OSDU data-partition id."
        )
        st.page_link(
            SETTINGS_PAGE_PATH,
            label="Open Instance Configuration",
            icon="⚙️",
        )
        return False

    return True


def _acquire_token(connection: ADMEConnection) -> str | None:
    """Acquire an ADME token, rendering an operator-safe error on failure."""
    try:
        if connection.auth_method == AuthMethod.USER_IMPERSONATION:
            return get_token(
                connection,
                user_auth_state=get_user_auth_state(st.session_state),
            )
        return get_token(connection)
    except AuthenticationError as exc:
        st.error(
            f"Could not acquire an ADME token: {exc}. "
            "Open Instance Configuration to sign in again or update "
            "credentials."
        )
        st.page_link(
            SETTINGS_PAGE_PATH,
            label="Open Instance Configuration",
            icon="⚙️",
        )
        return None
    except Exception as exc:  # noqa: BLE001 - never expose raw auth details
        st.error(
            f"Unexpected error acquiring an ADME token: "
            f"{type(exc).__name__}. Open Instance Configuration to "
            "verify your connection."
        )
        st.page_link(
            SETTINGS_PAGE_PATH,
            label="Open Instance Configuration",
            icon="⚙️",
        )
        return None


# ---------------------------------------------------------------------------
# Autorun-once
# ---------------------------------------------------------------------------


def _autorun_once(connection: ADMEConnection, token: str) -> None:
    """On first page load (or after Refresh), load kinds + first page."""
    if st.session_state.get(SEARCH_AUTORUN_DONE_KEY):
        return
    _refresh_all(connection, token)
    st.session_state[SEARCH_AUTORUN_DONE_KEY] = True


def _refresh_all(connection: ADMEConnection, token: str) -> None:
    """Reload kind dropdown + the current page of results."""
    with st.spinner("Loading kinds…"):
        kind_result = list_kinds(connection, token)
    _append_history_kinds(kind_result)
    options = [WILDCARD_KIND]
    if kind_result.ok:
        options.extend(k for k in kind_result.kinds if k != WILDCARD_KIND)
    st.session_state[SEARCH_KIND_OPTIONS_KEY] = options
    # Prune selections to only kinds that still exist.
    selections = [
        k
        for k in st.session_state.get(SEARCH_KIND_SELECTIONS_KEY, [])
        if k in options
    ]

    kind, query = _resolve_search_params(
        selections,
        st.session_state.get(SEARCH_RESOLVED_QUERY_KEY, ""),
    )
    st.session_state[SEARCH_KIND_FILTER_KEY] = kind
    offset = st.session_state.get(SEARCH_PAGE_OFFSET_KEY, 0)
    _run_search(
        connection, token, kind=kind, query=query, offset=offset
    )


# ---------------------------------------------------------------------------
# Toolbar
# ---------------------------------------------------------------------------


def _render_toolbar(connection: ADMEConnection, token: str) -> None:
    """Render the Refresh / Kind / Query / Search row."""
    cols = st.columns([1, 3, 5, 1])
    with cols[0]:
        refresh_clicked = st.button(
            "🔄 Refresh",
            key="search_refresh_button",
            help="Reload the kind list and re-run the current search.",
        )
    with cols[1]:
        kind_options = st.session_state.get(
            SEARCH_KIND_OPTIONS_KEY, [WILDCARD_KIND]
        ) or [WILDCARD_KIND]
        # Wildcard is implicit (empty selection = all kinds).
        selectable = [k for k in kind_options if k != WILDCARD_KIND]
        st.multiselect(
            "Kind filter",
            options=selectable,
            key=SEARCH_KIND_SELECTIONS_KEY,
            placeholder="All kinds (wildcard)",
            help="Select one or more OSDU kinds. Leave empty for all.",
        )
    with cols[2]:
        st.text_input(
            "Free-text query (Lucene syntax)",
            key=SEARCH_QUERY_TEXT_KEY,
            placeholder='data.SpudDate:[2020-01-01 TO 2024-12-31]',
            help=(
                "Leave blank to browse. Use Lucene syntax to filter — "
                "see the OSDU search docs linked below."
            ),
        )
    with cols[3]:
        st.markdown("&nbsp;")  # vertical alignment with labelled inputs
        search_clicked = st.button(
            "🔍 Search",
            key="search_run_button",
            type="primary",
        )

    if _HAS_AGGREGATION:
        st.checkbox(
            "Aggregate by kind",
            key=SEARCH_AGGREGATE_KEY,
        )

    st.caption(
        f"[OSDU search syntax reference]({OSDU_SEARCH_SYNTAX_DOCS_URL})"
    )

    # Caption for empty kind list (post-autorun).
    kind_options = st.session_state.get(SEARCH_KIND_OPTIONS_KEY, [])
    if (
        st.session_state.get(SEARCH_AUTORUN_DONE_KEY)
        and len(kind_options) <= 1
    ):
        st.caption("Kind list unavailable — use free-text query.")

    if refresh_clicked:
        st.session_state[SEARCH_PAGE_OFFSET_KEY] = 0
        st.session_state[SEARCH_RESOLVED_QUERY_KEY] = (
            st.session_state.get(SEARCH_QUERY_TEXT_KEY, "") or ""
        )
        _refresh_all(connection, token)
        st.rerun()

    if search_clicked:
        st.session_state[SEARCH_PAGE_OFFSET_KEY] = 0
        st.session_state[SEARCH_SELECTED_RECORD_ID_KEY] = None
        st.session_state[SEARCH_RESOLVED_QUERY_KEY] = (
            st.session_state.get(SEARCH_QUERY_TEXT_KEY, "") or ""
        )
        selections = list(
            st.session_state.get(SEARCH_KIND_SELECTIONS_KEY, [])
        )
        kind, query = _resolve_search_params(
            selections, st.session_state[SEARCH_RESOLVED_QUERY_KEY]
        )
        st.session_state[SEARCH_KIND_FILTER_KEY] = kind
        _run_search(
            connection,
            token,
            kind=kind,
            query=query,
            offset=0,
        )
        st.rerun()


# ---------------------------------------------------------------------------
# Kind resolution helpers
# ---------------------------------------------------------------------------


def _resolve_kind(selections: list[str]) -> str:
    """Resolve multiselect selections to a single kind string for the API."""
    if not selections:
        return WILDCARD_KIND
    if len(selections) == 1:
        return selections[0]
    return WILDCARD_KIND


def _resolve_search_params(
    selections: list[str], user_query: str
) -> tuple[str, str]:
    """Return ``(kind, query)`` incorporating multi-kind constraints.

    When multiple kinds are selected and ``build_multi_kind_query`` is
    available, kind constraints are folded into the Lucene query.
    """
    kind = _resolve_kind(selections)
    if len(selections) <= 1:
        return kind, user_query
    if _HAS_AGGREGATION:
        kind_q = build_multi_kind_query(selections)  # type: ignore[name-defined]
        if user_query:
            return kind, f"({kind_q}) AND ({user_query})"
        return kind, kind_q
    return kind, user_query


def _current_search_params() -> tuple[str, str]:
    """Return ``(kind, query)`` from current session state."""
    selections = list(
        st.session_state.get(SEARCH_KIND_SELECTIONS_KEY, [])
    )
    user_query = st.session_state.get(SEARCH_RESOLVED_QUERY_KEY, "")
    return _resolve_search_params(selections, user_query)


# ---------------------------------------------------------------------------
# Search execution
# ---------------------------------------------------------------------------


def _run_search(
    connection: ADMEConnection,
    token: str,
    *,
    kind: str,
    query: str,
    offset: int,
) -> None:
    """Call search_records and persist the outcome to session_state."""
    # Cap requested offset+limit to OSDU's hard ceiling so we never
    # raise ValueError from the service module.
    limit = SEARCH_PAGE_SIZE
    if offset + limit > MAX_OFFSET_PLUS_LIMIT:
        limit = max(1, MAX_OFFSET_PLUS_LIMIT - offset)

    aggregate = bool(
        st.session_state.get(SEARCH_AGGREGATE_KEY, False)
        and _HAS_AGGREGATION
    )

    if aggregate:
        with st.spinner("Aggregating…"):
            agg_result = search_with_aggregation(  # type: ignore[name-defined]
                connection,
                token,
                kind=kind,
                query=query or None,
                aggregate_by="kind",
            )
        agg_list = getattr(agg_result, "aggregations", []) or []
        if agg_result.ok and agg_list:
            st.session_state[SEARCH_AGGREGATION_RESULTS_KEY] = [
                {"Kind": b.key, "Count": b.count} for b in agg_list
            ]
        else:
            st.session_state[SEARCH_AGGREGATION_RESULTS_KEY] = []
    else:
        st.session_state[SEARCH_AGGREGATION_RESULTS_KEY] = []

    with st.spinner("Loading records…"):
        result = search_records(
            connection,
            token,
            kind=kind,
            query=query or None,
            limit=limit,
            offset=offset,
        )

    _append_history_search(result)

    if result.ok:
        st.session_state[SEARCH_RESULTS_KEY] = list(result.records)
        st.session_state[SEARCH_TOTAL_COUNT_KEY] = result.total_count
        st.session_state[SEARCH_PAGE_OFFSET_KEY] = offset
        # A new successful page invalidates any previously-selected row
        # since the row indices no longer line up.
        # (We don't clear the cache — operators may want to look back at
        # full records they already fetched.)
        st.session_state[SEARCH_LAST_ERROR_KEY] = None
    else:
        st.session_state[SEARCH_LAST_ERROR_KEY] = _format_search_error(
            result
        )


def _format_search_error(result: SearchPageResult) -> str:
    msg = result.error_message or "Unknown error."
    detail_bits: list[str] = []
    if result.http_status is not None:
        detail_bits.append(f"HTTP {result.http_status}")
    if result.correlation_id:
        detail_bits.append(f"correlation `{result.correlation_id}`")
    suffix = (" · " + " · ".join(detail_bits)) if detail_bits else ""
    return f"❌ Search failed: {msg}{suffix}"


# ---------------------------------------------------------------------------
# Aggregation summary (issue #26)
# ---------------------------------------------------------------------------


def _render_aggregation_summary() -> None:
    """Show kind-level record counts when aggregation is active."""
    agg = st.session_state.get(SEARCH_AGGREGATION_RESULTS_KEY, [])
    if not agg:
        return
    st.markdown("📊 **Records by kind:**")
    st.dataframe(
        pd.DataFrame(agg),
        use_container_width=True,
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Query builder (issue #27)
# ---------------------------------------------------------------------------

_QUERY_OPERATORS: list[str] = [
    "contains",
    "exact",
    "exists",
    "range",
    "equals",
]


def _build_lucene_query(
    clauses: list[dict[str, str]], combinator: str
) -> str:
    """Build a Lucene query string from clause dicts."""
    parts: list[str] = []
    for clause in clauses:
        field = clause.get("field", "")
        operator = clause.get("operator", "")
        value = clause.get("value", "")
        if not field:
            continue
        # Normalize: SchemaField.path values typically come prefixed with
        # "data." already. Strip it so we don't end up with "data.data.X".
        if field.startswith("data."):
            field = field[len("data."):]
        if operator == "contains":
            parts.append(f"data.{field}:*{value}*")
        elif operator == "exact":
            parts.append(f'data.{field}.keyword:"{value}"')
        elif operator == "exists":
            parts.append(f"_exists_:data.{field}")
        elif operator == "range":
            range_parts = [v.strip() for v in value.split(",", 1)]
            if len(range_parts) == 2:
                parts.append(
                    f"data.{field}:[{range_parts[0]} TO {range_parts[1]}]"
                )
            else:
                parts.append(f"data.{field}:[{value} TO *]")
        elif operator == "equals":
            parts.append(f'data.{field}:"{value}"')
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    joined = f" {combinator} ".join(parts)
    return f"({joined})"


def _render_query_builder() -> None:
    """Render the optional query-builder expander (issue #27)."""
    if not _HAS_FIELD_BUILDER:
        return

    with st.expander("🔨 Query Builder", expanded=False):
        _render_query_builder_inner()


def _render_query_builder_inner() -> None:
    """Query builder controls inside the expander."""
    try:
        available_kinds = list_schema_kinds()  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001
        available_kinds = []

    if not available_kinds:
        st.caption("No vendored schemas found for field discovery.")
        return

    selected_kind = st.selectbox(
        "Schema kind",
        options=[""] + available_kinds,
        key=QUERY_BUILDER_KIND_KEY,
        format_func=lambda k: "(select a schema)" if not k else k,
    )

    # Load fields when a kind is chosen.
    fields: list[Any] = []
    if selected_kind:
        try:
            schema = load_schema(selected_kind)  # type: ignore[name-defined]
            fields = extract_schema_fields(schema)  # type: ignore[name-defined]
            st.session_state[QUERY_BUILDER_FIELDS_KEY] = fields
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Could not load schema fields: {exc}")
    if not fields:
        fields = list(st.session_state.get(QUERY_BUILDER_FIELDS_KEY, []))

    field_names = [f.path for f in fields] if fields else []

    # Combinator
    combinator: str = st.radio(
        "Combinator",
        options=["AND", "OR"],
        key=QUERY_BUILDER_COMBINATOR_KEY,
        horizontal=True,
    )

    # Current clauses
    clauses: list[dict[str, str]] = list(
        st.session_state.get(QUERY_BUILDER_CLAUSES_KEY, [])
    )

    # Display existing clauses with remove buttons.
    remove_idx: int | None = None
    for i, clause in enumerate(clauses):
        c_cols = st.columns([3, 2, 3, 1])
        with c_cols[0]:
            st.text(clause.get("field", "?"))
        with c_cols[1]:
            st.text(clause.get("operator", "?"))
        with c_cols[2]:
            st.text(clause.get("value", "") or "(any)")
        with c_cols[3]:
            if st.button("🗑️", key=f"qb_remove_{i}"):
                remove_idx = i

    if remove_idx is not None:
        clauses.pop(remove_idx)
        st.session_state[QUERY_BUILDER_CLAUSES_KEY] = clauses
        st.rerun()

    # Add-clause row.
    st.markdown("**Add clause:**")
    a_cols = st.columns([3, 2, 3, 1])
    with a_cols[0]:
        new_field: str = st.selectbox(
            "Field",
            options=[""] + field_names,
            key="qb_new_field",
            label_visibility="collapsed",
        )
    with a_cols[1]:
        new_op: str = st.selectbox(
            "Operator",
            options=_QUERY_OPERATORS,
            key="qb_new_op",
            label_visibility="collapsed",
        )
    with a_cols[2]:
        new_val: str = st.text_input(
            "Value",
            key="qb_new_value",
            label_visibility="collapsed",
        )
    with a_cols[3]:
        if st.button("Add clause", key="qb_add"):
            clauses.append(
                {
                    "field": new_field,
                    "operator": new_op,
                    "value": "" if new_op == "exists" else new_val,
                }
            )
            st.session_state[QUERY_BUILDER_CLAUSES_KEY] = clauses
            st.rerun()

    # Preview + apply.
    lucene = _build_lucene_query(clauses, combinator)
    if lucene:
        st.code(lucene, language=None)
    else:
        st.caption("Add clauses above to build a query.")

    if st.button(
        "Apply to search", key="qb_apply", disabled=not lucene
    ):
        st.session_state[SEARCH_QUERY_TEXT_KEY] = lucene
        st.session_state[SEARCH_RESOLVED_QUERY_KEY] = lucene
        st.rerun()

    st.caption("_returnedFields projection picker — coming soon._")


# ---------------------------------------------------------------------------
# Results dataframe
# ---------------------------------------------------------------------------


def _render_results_section() -> None:
    """Render 'Showing N of M' caption + dataframe + row selector."""
    results: list[RecordSummary] = list(
        st.session_state.get(SEARCH_RESULTS_KEY, [])
    )
    total: int | None = st.session_state.get(SEARCH_TOTAL_COUNT_KEY)
    offset: int = int(st.session_state.get(SEARCH_PAGE_OFFSET_KEY, 0))

    if not results:
        if st.session_state.get(SEARCH_AUTORUN_DONE_KEY):
            st.info("No records matched.")
        return

    page_size = len(results)
    if total is not None:
        st.caption(
            f"Showing {offset + 1}–{offset + page_size} of "
            f"{total:,} records"
        )
    else:
        st.caption(
            f"Showing {offset + 1}–{offset + page_size} records "
            "(total unknown)"
        )

    rows = [
        {
            "id": rec.id,
            "kind": rec.kind,
            "createTime": rec.create_time or "",
            "source preview": _preview_source(rec.source),
        }
        for rec in results
    ]
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )

    # st.dataframe row-click is unreliable in Streamlit 1.57; use a
    # selectbox of ids instead so selection is deterministic across
    # reruns and works in tests.
    ids = [rec.id for rec in results]
    current_selection = st.session_state.get(
        SEARCH_SELECTED_RECORD_ID_KEY
    )
    selectbox_options: list[str] = ["—"] + ids
    if current_selection in ids:
        default_index = ids.index(current_selection) + 1
    else:
        default_index = 0
    chosen = st.selectbox(
        "Select a record to inspect",
        options=selectbox_options,
        index=default_index,
        key="search_record_selector",
    )
    # `chosen` is a fresh widget output, not the bound key for the
    # selection itself — assigning to SEARCH_SELECTED_RECORD_ID_KEY is
    # safe even though it controls the panel below.
    st.session_state[SEARCH_SELECTED_RECORD_ID_KEY] = (
        None if chosen == "—" else chosen
    )


def _preview_source(source: dict[str, Any]) -> str:
    """Compact one-line preview of the record source block."""
    if not source:
        return ""
    try:
        encoded = json.dumps(source, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        encoded = str(source)
    if len(encoded) > SEARCH_DATA_PREVIEW_CHARS:
        return encoded[: SEARCH_DATA_PREVIEW_CHARS - 1] + "…"
    return encoded


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def _flatten_source(source: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict with dot-notation keys.

    >>> _flatten_source({"data": {"FacilityName": "X", "Nested": {"A": 1}}})
    {'data.FacilityName': 'X', 'data.Nested.A': 1}
    """
    flat: dict[str, Any] = {}
    for key, value in source.items():
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(_flatten_source(value, full_key))
        else:
            flat[full_key] = value
    return flat


def _records_to_csv(records: list[RecordSummary]) -> str:
    """Convert a list of RecordSummary to CSV string."""
    if not records:
        return ""

    # Collect all data.* field names across all records.
    all_data_keys: dict[str, None] = {}
    rows: list[dict[str, Any]] = []
    for rec in records:
        flat = _flatten_source(rec.source) if rec.source else {}
        row: dict[str, Any] = {
            "id": rec.id,
            "kind": rec.kind,
            "createTime": rec.create_time or "",
            "version": rec.version if rec.version is not None else "",
        }
        for k, v in flat.items():
            all_data_keys[k] = None
            row[k] = v
        rows.append(row)

    fieldnames = ["id", "kind", "createTime", "version"] + list(all_data_keys)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def _records_to_json(records: list[RecordSummary]) -> str:
    """Convert a list of RecordSummary to pretty-printed JSON string."""
    return json.dumps(
        [rec.source for rec in records],
        indent=2,
        default=str,
        ensure_ascii=False,
    )


def _set_export_abort() -> None:
    """``on_click`` callback — sets the graceful abort flag."""
    st.session_state[EXPORT_ABORT_KEY] = True


def _render_export_section(
    connection: ADMEConnection, token: str
) -> None:
    """Render export buttons below the results when results are present."""
    results: list[RecordSummary] = list(
        st.session_state.get(SEARCH_RESULTS_KEY, [])
    )
    if not results:
        return

    total: int | None = st.session_state.get(SEARCH_TOTAL_COUNT_KEY)
    is_large = total is not None and total > len(results)

    # --- Done / error states from a previous export run --------------------
    export_status = st.session_state.get(EXPORT_STATUS_KEY, "idle")

    if export_status == "done":
        accumulated: list[RecordSummary] = list(
            st.session_state.get(EXPORT_RECORDS_KEY, [])
        )
        fmt = st.session_state.get(EXPORT_FORMAT_KEY, "csv")
        if accumulated:
            data = (
                _records_to_csv(accumulated)
                if fmt == "csv"
                else _records_to_json(accumulated)
            )
            filename = f"search_export.{fmt}"
            mime = "text/csv" if fmt == "csv" else "application/json"
            st.download_button(
                f"📥 Download {fmt.upper()} ({len(accumulated):,} records)",
                data=data,
                file_name=filename,
                mime=mime,
                key="export_download_done",
            )
        if st.button("Clear export", key="export_clear"):
            st.session_state[EXPORT_STATUS_KEY] = "idle"
            st.session_state[EXPORT_RECORDS_KEY] = []
            st.session_state[EXPORT_ERROR_KEY] = None
            st.session_state[EXPORT_ABORT_KEY] = False
            st.rerun()
        return

    if export_status == "error":
        err_msg = st.session_state.get(EXPORT_ERROR_KEY) or "Unknown error."
        accumulated = list(st.session_state.get(EXPORT_RECORDS_KEY, []))
        st.error(err_msg)
        if accumulated:
            fmt = st.session_state.get(EXPORT_FORMAT_KEY, "csv")
            data = (
                _records_to_csv(accumulated)
                if fmt == "csv"
                else _records_to_json(accumulated)
            )
            filename = f"search_export_partial.{fmt}"
            mime = "text/csv" if fmt == "csv" else "application/json"
            st.download_button(
                f"📥 Download partial {fmt.upper()} ({len(accumulated):,} records)",
                data=data,
                file_name=filename,
                mime=mime,
                key="export_download_partial",
            )
        if st.button("Clear export", key="export_clear_error"):
            st.session_state[EXPORT_STATUS_KEY] = "idle"
            st.session_state[EXPORT_RECORDS_KEY] = []
            st.session_state[EXPORT_ERROR_KEY] = None
            st.session_state[EXPORT_ABORT_KEY] = False
            st.rerun()
        return

    # --- Small result set: direct download from current page ---------------
    if not is_large:
        cols = st.columns([1, 1, 6])
        with cols[0]:
            csv_data = _records_to_csv(results)
            st.download_button(
                "📥 Export CSV",
                data=csv_data,
                file_name="search_export.csv",
                mime="text/csv",
                key="export_csv_small",
            )
        with cols[1]:
            json_data = _records_to_json(results)
            st.download_button(
                "📥 Export JSON",
                data=json_data,
                file_name="search_export.json",
                mime="application/json",
                key="export_json_small",
            )
        return

    # --- Large result set: cursor export required --------------------------
    if not _CURSOR_EXPORT_AVAILABLE:
        st.caption(
            "Export for large result sets is not yet available "
            "(cursor pagination pending)."
        )
        return

    assert total is not None  # mypy — is_large guarantees total > len
    st.caption(
        f"Export will fetch all {total:,} records using cursor pagination."
    )
    if total > 10_000:
        st.warning("Large export — this may take a while.")

    cols = st.columns([1, 1, 6])
    with cols[0]:
        csv_clicked = st.button(
            "📥 Export CSV",
            key="export_csv_large",
        )
    with cols[1]:
        json_clicked = st.button(
            "📥 Export JSON",
            key="export_json_large",
        )

    if csv_clicked or json_clicked:
        fmt = "csv" if csv_clicked else "json"
        st.session_state[EXPORT_FORMAT_KEY] = fmt
        st.session_state[EXPORT_ABORT_KEY] = False
        _run_cursor_export(connection, token, fmt)
        st.rerun()


def _run_cursor_export(
    connection: ADMEConnection,
    token: str,
    fmt: str,
) -> None:
    """Walk the full result set via cursor pagination and store results."""
    kind, query_str = _current_search_params()
    query = query_str or None

    st.session_state[EXPORT_STATUS_KEY] = "exporting"
    st.session_state[EXPORT_RECORDS_KEY] = []
    st.session_state[EXPORT_ERROR_KEY] = None

    accumulated: list[RecordSummary] = []
    total_count: int | None = None

    st.button(
        "⏹️ Abort export",
        key="export_abort_btn",
        on_click=_set_export_abort,
        help="Stop after the current page finishes.",
    )
    progress_bar = st.progress(0.0, text="Exporting records…")

    try:
        iterator = export_all_records(  # type: ignore[name-defined]
            connection,
            token,
            kind=kind,
            query=query,
        )
        for page in iterator:
            if not page.ok:
                n = len(accumulated)
                st.session_state[EXPORT_STATUS_KEY] = "error"
                st.session_state[EXPORT_RECORDS_KEY] = accumulated
                st.session_state[EXPORT_ERROR_KEY] = (
                    f"Export failed after {n:,} records. "
                    f"Error: {page.error_message or 'Unknown error.'}"
                )
                return

            accumulated.extend(page.records)
            if total_count is None and page.total_count is not None:
                total_count = page.total_count

            if total_count and total_count > 0:
                progress = min(len(accumulated) / total_count, 1.0)
                progress_bar.progress(
                    progress,
                    text=f"Exported {len(accumulated):,} of {total_count:,}…",
                )

            st.session_state[EXPORT_RECORDS_KEY] = list(accumulated)

            if st.session_state.get(EXPORT_ABORT_KEY):
                st.session_state[EXPORT_STATUS_KEY] = "done"
                st.warning(
                    f"⏹️ Export aborted after {len(accumulated):,} records."
                )
                return

            if not page.has_more:
                break

    except Exception as exc:  # noqa: BLE001 - operator-safe summary
        n = len(accumulated)
        st.session_state[EXPORT_STATUS_KEY] = "error"
        st.session_state[EXPORT_RECORDS_KEY] = accumulated
        st.session_state[EXPORT_ERROR_KEY] = (
            f"Export failed after {n:,} records. "
            f"Error: {type(exc).__name__}: {exc}"
        )
        return

    st.session_state[EXPORT_STATUS_KEY] = "done"
    st.session_state[EXPORT_RECORDS_KEY] = accumulated


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _render_pagination(connection: ADMEConnection, token: str) -> None:
    """Render Prev / Page N / Next, gated by total_count and OSDU ceiling."""
    results: list[RecordSummary] = list(
        st.session_state.get(SEARCH_RESULTS_KEY, [])
    )
    if not results:
        return

    total: int | None = st.session_state.get(SEARCH_TOTAL_COUNT_KEY)
    offset: int = int(st.session_state.get(SEARCH_PAGE_OFFSET_KEY, 0))
    page_size = len(results)

    next_offset = offset + page_size
    at_ceiling = next_offset + SEARCH_PAGE_SIZE > MAX_OFFSET_PLUS_LIMIT
    past_total = total is not None and next_offset >= total

    prev_disabled = offset <= 0
    next_disabled = past_total or at_ceiling

    page_number = (offset // SEARCH_PAGE_SIZE) + 1

    cols = st.columns([1, 1, 1, 6])
    with cols[0]:
        prev_clicked = st.button(
            "« Prev",
            key="search_prev_button",
            disabled=prev_disabled,
        )
    with cols[1]:
        st.markdown(f"**Page {page_number}**")
    with cols[2]:
        next_clicked = st.button(
            "Next »",
            key="search_next_button",
            disabled=next_disabled,
        )

    if at_ceiling:
        st.caption("OSDU caps offset+limit at 10,000.")

    if prev_clicked:
        new_offset = max(0, offset - SEARCH_PAGE_SIZE)
        kind, query = _current_search_params()
        st.session_state[SEARCH_SELECTED_RECORD_ID_KEY] = None
        _run_search(
            connection, token, kind=kind, query=query, offset=new_offset
        )
        st.rerun()

    if next_clicked:
        new_offset = next_offset
        kind, query = _current_search_params()
        st.session_state[SEARCH_SELECTED_RECORD_ID_KEY] = None
        _run_search(
            connection, token, kind=kind, query=query, offset=new_offset
        )
        st.rerun()


# ---------------------------------------------------------------------------
# Selected record detail
# ---------------------------------------------------------------------------


def _render_selected_record(
    connection: ADMEConnection, token: str
) -> None:
    """Render the detail panel for the currently-selected record."""
    record_id = st.session_state.get(SEARCH_SELECTED_RECORD_ID_KEY)
    if not record_id:
        return

    summaries: list[RecordSummary] = list(
        st.session_state.get(SEARCH_RESULTS_KEY, [])
    )
    summary = next((r for r in summaries if r.id == record_id), None)
    if summary is None:
        return

    st.divider()
    st.subheader("📄 Selected record")
    st.markdown(
        "\n".join(
            [
                f"- **id:** `{summary.id}`",
                f"- **kind:** `{summary.kind}`",
                f"- **createTime:** "
                f"`{summary.create_time or '—'}`",
                f"- **version:** "
                f"`{summary.version if summary.version is not None else '—'}`",
            ]
        )
    )

    with st.expander("Search hit JSON (from results)"):
        st.json(summary.source or {})

    cache: dict[str, dict] = st.session_state.get(
        SEARCH_FULL_RECORD_CACHE_KEY, {}
    )
    cached = cache.get(summary.id)
    button_label = (
        "🔄 Refresh full record" if cached else "📥 Fetch full record"
    )
    if st.button(button_label, key="search_fetch_full_record"):
        _fetch_full_record(connection, token, summary.id)
        st.rerun()

    if cached:
        with st.expander("Full record (from Storage)"):
            st.json(cached)


def _fetch_full_record(
    connection: ADMEConnection, token: str, record_id: str
) -> None:
    """Fetch the full record from Storage and cache it."""
    with st.spinner("Fetching full record…"):
        result = get_record(connection, token, record_id)
    _append_history_get_record(result)

    if result.ok and result.record is not None:
        cache: dict[str, dict] = dict(
            st.session_state.get(SEARCH_FULL_RECORD_CACHE_KEY, {})
        )
        cache[record_id] = result.record
        st.session_state[SEARCH_FULL_RECORD_CACHE_KEY] = cache
        return

    if result.http_status == 404:
        st.session_state[SEARCH_LAST_ERROR_KEY] = (
            f"⚠️ Record `{record_id}` not found or not visible to your "
            "identity."
        )
        return

    msg = result.error_message or "Unknown error."
    detail_bits: list[str] = []
    if result.http_status is not None:
        detail_bits.append(f"HTTP {result.http_status}")
    if result.correlation_id:
        detail_bits.append(f"correlation `{result.correlation_id}`")
    suffix = (" · " + " · ".join(detail_bits)) if detail_bits else ""
    st.session_state[SEARCH_LAST_ERROR_KEY] = (
        f"❌ Could not fetch record `{record_id}`: {msg}{suffix}"
    )


# ---------------------------------------------------------------------------
# History + latency chart
# ---------------------------------------------------------------------------


def _render_history() -> None:
    """Render the history dataframe + latency chart."""
    history = list(st.session_state.get(SEARCH_HISTORY_KEY, []))
    if not history:
        return

    st.divider()
    st.subheader("📊 Session history")

    cols = st.columns([1, 7])
    with cols[0]:
        if st.button("Clear history", key="search_clear_history"):
            st.session_state[SEARCH_HISTORY_KEY] = []
            st.rerun()

    recent = history[-HISTORY_DISPLAY_LIMIT:]
    rows = [
        {
            "timestamp": entry.get("timestamp", ""),
            "endpoint": entry.get("endpoint", ""),
            "ok": entry.get("ok", False),
            "http_status": entry.get("http_status"),
            "latency_ms": f"{float(entry.get('latency_ms', 0.0)):.1f}",
            "correlation_id": entry.get("correlation_id") or "—",
            "error_message": entry.get("error_message") or "",
        }
        for entry in recent
    ]
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )

    chart_rows = [
        {
            "timestamp": entry.get("timestamp", ""),
            "latency_ms": float(entry.get("latency_ms", 0.0)),
        }
        for entry in recent
    ]
    chart_df = pd.DataFrame(chart_rows)
    if not chart_df.empty:
        st.line_chart(chart_df, x="timestamp", y="latency_ms")


def _append_history(
    *,
    endpoint: str,
    ok: bool,
    http_status: int | None,
    latency_ms: float,
    correlation_id: str | None,
    error_message: str | None,
) -> None:
    """Append one history row. Append-only within the session."""
    history = list(st.session_state.get(SEARCH_HISTORY_KEY, []))
    history.append(
        {
            "timestamp": datetime.now(tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "endpoint": endpoint,
            "ok": ok,
            "http_status": http_status,
            "latency_ms": round(float(latency_ms), 1),
            "correlation_id": correlation_id,
            "error_message": error_message,
        }
    )
    st.session_state[SEARCH_HISTORY_KEY] = history


def _append_history_kinds(result: KindAggregationResult) -> None:
    _append_history(
        endpoint=LABEL_LIST_KINDS,
        ok=result.ok,
        http_status=result.http_status,
        latency_ms=result.latency_ms,
        correlation_id=result.correlation_id,
        error_message=result.error_message,
    )


def _append_history_search(result: SearchPageResult) -> None:
    _append_history(
        endpoint=f"{LABEL_SEARCH}.{result.kind}",
        ok=result.ok,
        http_status=result.http_status,
        latency_ms=result.latency_ms,
        correlation_id=result.correlation_id,
        error_message=result.error_message,
    )


def _append_history_get_record(result: RecordDetailResult) -> None:
    _append_history(
        endpoint=LABEL_GET_RECORD,
        ok=result.ok,
        http_status=result.http_status,
        latency_ms=result.latency_ms,
        correlation_id=result.correlation_id,
        error_message=result.error_message,
    )


# ---------------------------------------------------------------------------
# Sticky error banner
# ---------------------------------------------------------------------------


def _render_sticky_error() -> None:
    """Render the persistent error banner (if any) above the toolbar."""
    message = st.session_state.get(SEARCH_LAST_ERROR_KEY)
    if not message:
        return
    cols = st.columns([8, 1])
    with cols[0]:
        st.error(message)
    with cols[1]:
        if st.button("Dismiss error", key="search_dismiss_error"):
            st.session_state[SEARCH_LAST_ERROR_KEY] = None
            st.rerun()


main()
