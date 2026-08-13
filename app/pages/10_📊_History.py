"""History page — local SQLite-backed run + upload audit log.

Reads ``~/.adme-ingestion-tool/run-history.db`` (see
:mod:`app.services.run_history`) and surfaces three tabs:

1. **Workflow runs** — every Manifest / Builder submit and its terminal
   outcome.
2. **File uploads** — every File-page upload that registered metadata.
3. **Actions** — manual purge, clear all, and a diagnostic readout.

The default partition filter is the current connection's partition;
the "Show all partitions" toggle widens to every partition the local
DB has seen (useful when switching ADME instances).

This page reads local state only — no ADME service calls, no auth
required. The pre-flight check exists solely so the page can show
"`{current_partition}`" in the header. When no connection is
configured we fall back to "all partitions" mode automatically.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
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
)
from app.models.osdu import (  # noqa: E402
    RunRow,
    UploadRow,
    WorkflowStatus,
)
from app.services.run_history import (  # noqa: E402
    clear_all,
    db_info,
    list_file_uploads,
    list_workflow_runs,
    purge_older_than,
)

SETTINGS_PAGE_PATH = "pages/1_⚙️_Instance_Configuration.py"

# --- Locked session-state keys ------------------------------------------
HISTORY_SHOW_ALL_PARTITIONS_KEY = "history_show_all_partitions"
HISTORY_STATUS_FILTER_KEY = "history_status_filter"
HISTORY_DATE_RANGE_KEY = "history_date_range"
HISTORY_LIMIT_KEY = "history_limit"
HISTORY_UPLOADS_DATE_RANGE_KEY = "history_uploads_date_range"
HISTORY_UPLOADS_LIMIT_KEY = "history_uploads_limit"
HISTORY_PURGE_DAYS_KEY = "history_purge_days"
HISTORY_PURGE_CONFIRM_KEY = "history_purge_confirm"
HISTORY_CLEAR_CONFIRM_KEY = "history_clear_confirm"

DEFAULT_LIMIT = 100
DEFAULT_PURGE_DAYS = 30

STATUS_LABEL_FINISHED = "Finished"
STATUS_LABEL_FAILED = "Failed"
STATUS_LABEL_IN_PROGRESS = "In progress"
STATUS_LABEL_UNKNOWN = "Unknown"

_STATUS_OPTIONS: tuple[str, ...] = (
    STATUS_LABEL_FINISHED,
    STATUS_LABEL_FAILED,
    STATUS_LABEL_IN_PROGRESS,
    STATUS_LABEL_UNKNOWN,
)

_STATUS_LABEL_TO_ENUM: dict[str, WorkflowStatus] = {
    STATUS_LABEL_FINISHED: WorkflowStatus.FINISHED,
    STATUS_LABEL_FAILED: WorkflowStatus.FAILED,
    STATUS_LABEL_IN_PROGRESS: WorkflowStatus.IN_PROGRESS,
    STATUS_LABEL_UNKNOWN: WorkflowStatus.UNKNOWN,
}


def main() -> None:
    """Render the History page."""
    st.set_page_config(
        page_title="History · ADME Control Plane",
        page_icon="📊",
        layout="wide",
    )
    st.title("📊 History")
    st.markdown(
        "Local audit log of workflow runs and file uploads from this "
        "machine. Stored in `~/.adme-ingestion-tool/run-history.db`. "
        "No ADME service calls — this view is offline-safe."
    )

    ensure_session_defaults(st.session_state)
    _ensure_page_defaults()

    connection = get_connection(st.session_state)
    current_partition = (
        connection.data_partition_id if connection is not None else ""
    )

    if not current_partition:
        st.info(
            "No data partition configured for this session. Showing "
            "all partitions in the local history DB."
        )
        st.page_link(
            SETTINGS_PAGE_PATH,
            label="Open Instance Configuration",
            icon="⚙️",
        )
        # Force show-all so the partition filter doesn't filter out
        # everything when no connection is configured.
        st.session_state[HISTORY_SHOW_ALL_PARTITIONS_KEY] = True

    runs_tab, uploads_tab, actions_tab = st.tabs(
        ["Workflow runs", "File uploads", "Actions"]
    )
    with runs_tab:
        _render_runs_tab(current_partition)
    with uploads_tab:
        _render_uploads_tab(current_partition)
    with actions_tab:
        _render_actions_tab()


def _ensure_page_defaults() -> None:
    """Initialize page-scoped session keys."""
    st.session_state.setdefault(HISTORY_SHOW_ALL_PARTITIONS_KEY, False)
    st.session_state.setdefault(HISTORY_STATUS_FILTER_KEY, [])
    st.session_state.setdefault(HISTORY_DATE_RANGE_KEY, ())
    st.session_state.setdefault(HISTORY_LIMIT_KEY, DEFAULT_LIMIT)
    st.session_state.setdefault(HISTORY_UPLOADS_DATE_RANGE_KEY, ())
    st.session_state.setdefault(HISTORY_UPLOADS_LIMIT_KEY, DEFAULT_LIMIT)
    st.session_state.setdefault(HISTORY_PURGE_DAYS_KEY, DEFAULT_PURGE_DAYS)
    st.session_state.setdefault(HISTORY_PURGE_CONFIRM_KEY, False)
    st.session_state.setdefault(HISTORY_CLEAR_CONFIRM_KEY, False)


# ---------------------------------------------------------------------------
# Tab 1 — Workflow runs
# ---------------------------------------------------------------------------


def _render_runs_tab(current_partition: str) -> None:
    """Render the workflow-runs tab."""
    show_all = _render_partition_header(
        current_partition, key_suffix="runs"
    )

    cols = st.columns([3, 2, 1])
    with cols[0]:
        statuses = st.multiselect(
            "Status",
            options=list(_STATUS_OPTIONS),
            key=HISTORY_STATUS_FILTER_KEY,
            help="Leave empty to show all statuses.",
        )
    with cols[1]:
        date_range = st.date_input(
            "Submitted within",
            value=st.session_state.get(HISTORY_DATE_RANGE_KEY, ()),
            key=HISTORY_DATE_RANGE_KEY,
        )
    with cols[2]:
        limit = st.number_input(
            "Max rows",
            min_value=1,
            max_value=10_000,
            value=int(st.session_state.get(HISTORY_LIMIT_KEY, DEFAULT_LIMIT)),
            step=50,
            key=HISTORY_LIMIT_KEY,
        )

    st.button(
        "🔄 Refresh", key="history_refresh_runs",
        help="Re-read the local DB.",
    )

    since, until = _date_range_to_bounds(date_range)
    partition_filter = None if show_all else (current_partition or None)

    rows: list[RunRow] = []
    seen: set[str] = set()
    statuses_to_query: list[WorkflowStatus | None]
    if statuses:
        statuses_to_query = [_STATUS_LABEL_TO_ENUM[s] for s in statuses]
    else:
        statuses_to_query = [None]

    for status_filter in statuses_to_query:
        chunk = list_workflow_runs(
            limit=int(limit),
            status=status_filter,
            since=since,
            until=until,
            data_partition_id=partition_filter,
        )
        for row in chunk:
            if row.run_id not in seen:
                seen.add(row.run_id)
                rows.append(row)

    rows.sort(key=lambda r: r.submitted_at, reverse=True)
    rows = rows[: int(limit)]

    if not rows:
        st.info(
            "No workflow runs yet — submit a manifest from the Manifest page."
        )
        return

    frame = pd.DataFrame(
        [
            {
                "When": _relative_time(row.submitted_at),
                "Submitted at (UTC)": row.submitted_at,
                "Kind": row.kind or "—",
                "Status": _status_emoji(row.status),
                "Latency": _format_latency_ms(row.latency_ms),
                "Run ID": _truncate(row.run_id, 18),
                "Correlation ID": _truncate(row.correlation_id, 18),
                "Source": row.submit_source,
                "Partition": row.data_partition_id,
                "Error": _truncate(row.error_message, 60),
            }
            for row in rows
        ]
    )
    st.dataframe(frame, use_container_width=True, hide_index=True)
    st.caption(f"Showing {len(rows)} row(s).")

    failures = [row for row in rows if row.error_message]
    if failures:
        with st.expander(f"Error details ({len(failures)} failures)"):
            for row in failures:
                st.markdown(
                    f"**`{row.run_id}`** "
                    f"({row.submitted_at}) — source: `{row.submit_source}`"
                )
                st.code(row.error_message or "", language="text")


# ---------------------------------------------------------------------------
# Tab 2 — File uploads
# ---------------------------------------------------------------------------


def _render_uploads_tab(current_partition: str) -> None:
    """Render the file-uploads tab."""
    show_all = _render_partition_header(
        current_partition, key_suffix="uploads"
    )

    cols = st.columns([3, 1])
    with cols[0]:
        date_range = st.date_input(
            "Uploaded within",
            value=st.session_state.get(HISTORY_UPLOADS_DATE_RANGE_KEY, ()),
            key=HISTORY_UPLOADS_DATE_RANGE_KEY,
        )
    with cols[1]:
        limit = st.number_input(
            "Max rows",
            min_value=1,
            max_value=10_000,
            value=int(
                st.session_state.get(
                    HISTORY_UPLOADS_LIMIT_KEY, DEFAULT_LIMIT
                )
            ),
            step=50,
            key=HISTORY_UPLOADS_LIMIT_KEY,
        )

    st.button(
        "🔄 Refresh", key="history_refresh_uploads",
        help="Re-read the local DB.",
    )

    since, until = _date_range_to_bounds(date_range)
    partition_filter = None if show_all else (current_partition or None)

    rows: list[UploadRow] = list_file_uploads(
        limit=int(limit),
        since=since,
        until=until,
        data_partition_id=partition_filter,
    )

    if not rows:
        st.info(
            "No uploads yet — register a file from the File page."
        )
        return

    frame = pd.DataFrame(
        [
            {
                "When": _relative_time(row.uploaded_at),
                "Uploaded at (UTC)": row.uploaded_at,
                "Display name": row.display_name,
                "Record ID": _truncate(row.record_id, 32),
                "FileSource": _truncate(row.file_source, 40),
                "Size": _humanize_bytes(row.size_bytes),
                "Partition": row.data_partition_id,
            }
            for row in rows
        ]
    )
    st.dataframe(frame, use_container_width=True, hide_index=True)
    st.caption(f"Showing {len(rows)} row(s).")


# ---------------------------------------------------------------------------
# Tab 3 — Actions
# ---------------------------------------------------------------------------


def _render_actions_tab() -> None:
    """Render the manual purge / clear-all / diagnostics tab."""
    st.subheader("Purge old rows")
    st.caption(
        "Deletes rows older than N days from BOTH workflow_runs and "
        "file_uploads. This is permanent."
    )
    days = st.number_input(
        "Purge rows older than (days)",
        min_value=1,
        max_value=10_000,
        value=int(
            st.session_state.get(HISTORY_PURGE_DAYS_KEY, DEFAULT_PURGE_DAYS)
        ),
        step=1,
        key=HISTORY_PURGE_DAYS_KEY,
    )
    confirm_purge = st.checkbox(
        "I understand this is permanent",
        key=HISTORY_PURGE_CONFIRM_KEY,
    )
    if st.button(
        "Purge now",
        type="primary",
        disabled=not confirm_purge,
        key="history_purge_button",
    ):
        runs_deleted, uploads_deleted = purge_older_than(days=int(days))
        st.success(
            f"✅ Purged {runs_deleted} workflow run(s) and "
            f"{uploads_deleted} upload(s) older than {int(days)} days."
        )
        st.session_state[HISTORY_PURGE_CONFIRM_KEY] = False

    st.divider()
    st.subheader("Clear all history")
    st.caption(
        "⚠️ Deletes every row in the local history DB. "
        "This cannot be undone."
    )
    confirm_clear = st.checkbox(
        "I really want to clear ALL local history",
        key=HISTORY_CLEAR_CONFIRM_KEY,
    )
    if st.button(
        "Clear all",
        type="primary",
        disabled=not confirm_clear,
        key="history_clear_button",
    ):
        clear_all()
        st.success("✅ Local history cleared.")
        st.session_state[HISTORY_CLEAR_CONFIRM_KEY] = False

    st.divider()
    st.subheader("Database info")
    info = db_info()
    st.markdown(
        "\n".join(
            [
                f"- **Path:** `{info['path']}`",
                f"- **Size:** {_humanize_bytes(info['size_bytes'])}",
                f"- **Schema version:** v{info['user_version']}",
                f"- **Workflow runs:** {info['runs']}",
                f"- **File uploads:** {info['uploads']}",
            ]
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_partition_header(
    current_partition: str, *, key_suffix: str
) -> bool:
    """Render the partition header + "Show all partitions" toggle.

    Returns ``True`` when the user has opted into showing all partitions.
    """
    if current_partition:
        st.markdown(
            f"Showing rows from partition `{current_partition}`"
        )
    else:
        st.markdown("Showing rows from **all partitions**")
    show_all = st.toggle(
        "Show all partitions",
        value=bool(
            st.session_state.get(HISTORY_SHOW_ALL_PARTITIONS_KEY, False)
        ),
        key=f"{HISTORY_SHOW_ALL_PARTITIONS_KEY}_{key_suffix}",
    )
    # Mirror the toggle to the shared key so both tabs stay aligned.
    st.session_state[HISTORY_SHOW_ALL_PARTITIONS_KEY] = bool(show_all)
    return bool(show_all)


def _date_range_to_bounds(value: Any) -> tuple[str | None, str | None]:
    """Convert a Streamlit ``date_input`` value to inclusive ISO bounds.

    Returns ``(None, None)`` when no range is set. A one-sided range
    applies only the lower bound; a single date object is treated as that
    whole UTC day.
    """
    if not value:
        return (None, None)
    if isinstance(value, (list, tuple)):
        if not value:
            return (None, None)
        start = value[0]
        end = value[1] if len(value) > 1 else None
    else:
        start = value
        end = value
    return (
        _date_to_iso_bound(start, end_of_day=False),
        _date_to_iso_bound(end, end_of_day=True),
    )


def _date_to_iso_bound(value: Any, *, end_of_day: bool) -> str | None:
    """Return a UTC ISO date bound for a Streamlit date value."""
    if value is None or not hasattr(value, "year"):
        return None
    if end_of_day:
        dt = datetime(
            value.year,
            value.month,
            value.day,
            23,
            59,
            59,
            tzinfo=UTC,
        )
    else:
        dt = datetime(value.year, value.month, value.day, tzinfo=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _relative_time(iso_utc: str) -> str:
    """Return a humanized "x ago" label for an ISO 8601 UTC timestamp."""
    try:
        ts = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError:
        return iso_utc
    delta = datetime.now(tz=UTC) - ts
    if delta < timedelta(seconds=0):
        return "just now"
    if delta < timedelta(minutes=1):
        return f"{int(delta.total_seconds())}s ago"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() / 60)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() / 3600)}h ago"
    return f"{delta.days}d ago"


def _status_emoji(status: WorkflowStatus) -> str:
    """Return a single-glyph status indicator."""
    if status == WorkflowStatus.FINISHED:
        return "✅ Finished"
    if status == WorkflowStatus.FAILED:
        return "❌ Failed"
    if status == WorkflowStatus.IN_PROGRESS:
        return "⏳ In progress"
    return "❓ Unknown"


def _format_latency_ms(value: int | None) -> str:
    """Render an ``int`` ms count as ``"1.23s"`` or ``"—"``."""
    if value is None:
        return "—"
    if value < 1000:
        return f"{value}ms"
    return f"{value / 1000:.2f}s"


def _truncate(value: str | None, length: int) -> str:
    """Truncate ``value`` to ``length`` chars with an ellipsis."""
    if not value:
        return "—"
    if len(value) <= length:
        return value
    return value[: length - 1] + "…"


def _humanize_bytes(value: int | None) -> str:
    """Render a byte count as ``"1.2 MB"``."""
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(value)} B"


if __name__ == "__main__":
    main()
