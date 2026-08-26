"""Tests for the ADME History page (``app/pages/10_📊_History.py``).

Loads the page via importlib with a ``StreamlitRecorder`` substituted
for ``streamlit``, seeds the run-history DB via the autouse env-var
override fixture, and asserts on recorded tab + dataframe + filter
behavior.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from app.connection_state import CONNECTION_KEY
from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import WorkflowStatus
from app.services import run_history
from tests.support.streamlit_recorder import StreamlitRecorder

HISTORY_PAGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "pages"
    / "10_📊_History.py"
)


def _load_page(
    recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    monkeypatch.setitem(sys.modules, "streamlit", recorder)
    module_name = "tests.generated_history_page"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, HISTORY_PAGE_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _connection(partition: str = "opendes") -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id=partition,
        auth_method=AuthMethod.SERVICE_PRINCIPAL,
        client_secret="placeholder",
    )


def _seed_run(
    run_id: str = "r1",
    *,
    partition: str = "opendes",
    submitted_at: str = "2026-05-12T15:00:00Z",
    submit_source: str = "manifest_page",
) -> None:
    run_history.record_workflow_submit(
        run_id=run_id,
        submitted_at=submitted_at,
        kind="osdu:wks:Manifest:1.0.0",
        correlation_id=f"corr-{run_id}",
        submit_source=submit_source,
        data_partition_id=partition,
    )


# ===========================================================================
# Render structure
# ===========================================================================


def test_page_renders_title_and_three_tabs(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    title_calls = streamlit_recorder.calls_named("title")
    assert any("History" in str(call.args[0]) for call in title_calls)

    tab_calls = streamlit_recorder.calls_named("tabs")
    assert tab_calls, "page must use st.tabs"
    labels = tab_calls[0].args[0]
    assert labels == ["Workflow runs", "File uploads", "Actions"]


def test_runs_tab_shows_empty_state_when_no_rows(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    info_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("info")
    ]
    assert any("No workflow runs yet" in m for m in info_messages)
    assert any("No uploads yet" in m for m in info_messages)


def test_runs_tab_renders_dataframe_when_rows_exist(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(run_id="r-shown", partition="opendes")
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    df_calls = streamlit_recorder.calls_named("dataframe")
    assert df_calls, "page must render the runs dataframe"
    frame = df_calls[0].args[0]
    rendered_run_ids = " ".join(str(v) for v in frame["Run ID"].tolist())
    assert "r-shown" in rendered_run_ids


# ===========================================================================
# Partition filter
# ===========================================================================


def test_default_partition_filter_excludes_other_partitions(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default: show current partition only."""
    _seed_run(run_id="r-here", partition="opendes")
    _seed_run(run_id="r-there", partition="other-partition")
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection(
        "opendes"
    )

    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    df_calls = streamlit_recorder.calls_named("dataframe")
    assert df_calls
    frame = df_calls[0].args[0]
    run_ids = frame["Run ID"].tolist()
    assert any("r-here" in str(v) for v in run_ids)
    assert not any("r-there" in str(v) for v in run_ids)


def test_show_all_partitions_toggle_includes_other_partitions(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(run_id="r-here", partition="opendes")
    _seed_run(run_id="r-there", partition="other-partition")
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection(
        "opendes"
    )
    # Toggle one of the two keyed toggles to True — the page mirrors any
    # show-all=True back into the shared key.
    streamlit_recorder.widget_values["Show all partitions"] = True

    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    df_calls = streamlit_recorder.calls_named("dataframe")
    frame = df_calls[0].args[0]
    run_ids = " ".join(str(v) for v in frame["Run ID"].tolist())
    assert "r-here" in run_ids
    assert "r-there" in run_ids


# ===========================================================================
# Date filters
# ===========================================================================


def test_runs_tab_date_range_excludes_rows_after_end_date(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(run_id="r-before", submitted_at="2026-05-11T23:59:59Z")
    _seed_run(run_id="r-in", submitted_at="2026-05-12T15:00:00Z")
    _seed_run(run_id="r-after", submitted_at="2026-05-13T00:00:00Z")
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    streamlit_recorder.widget_values["Submitted within"] = (
        date(2026, 5, 12),
        date(2026, 5, 12),
    )

    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    frame = streamlit_recorder.calls_named("dataframe")[0].args[0]
    run_ids = " ".join(str(v) for v in frame["Run ID"].tolist())
    assert "r-in" in run_ids
    assert "r-before" not in run_ids
    assert "r-after" not in run_ids


def test_uploads_tab_date_range_excludes_rows_after_end_date(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_history.record_file_upload(
        record_id="u-before",
        uploaded_at="2026-05-11T23:59:59Z",
        display_name="before.las",
        file_source="/staging/before",
        size_bytes=1,
        data_partition_id="opendes",
    )
    run_history.record_file_upload(
        record_id="u-in",
        uploaded_at="2026-05-12T15:00:00Z",
        display_name="in.las",
        file_source="/staging/in",
        size_bytes=1,
        data_partition_id="opendes",
    )
    run_history.record_file_upload(
        record_id="u-after",
        uploaded_at="2026-05-13T00:00:00Z",
        display_name="after.las",
        file_source="/staging/after",
        size_bytes=1,
        data_partition_id="opendes",
    )
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    streamlit_recorder.widget_values["Uploaded within"] = (
        date(2026, 5, 12),
        date(2026, 5, 12),
    )

    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    df_calls = streamlit_recorder.calls_named("dataframe")
    frame = next(call.args[0] for call in df_calls if "Display name" in call.args[0])
    display_names = " ".join(str(v) for v in frame["Display name"].tolist())
    assert "in.las" in display_names
    assert "before.las" not in display_names
    assert "after.las" not in display_names


# ===========================================================================
# Status / source columns
# ===========================================================================


def test_runs_tab_renders_status_emoji_and_source(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(run_id="r-done", submit_source="builder")
    run_history.record_workflow_finish(
        run_id="r-done",
        finished_at="2026-05-12T15:10:00Z",
        status=WorkflowStatus.FINISHED,
        latency_ms=600_000,
    )
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    frame = streamlit_recorder.calls_named("dataframe")[0].args[0]
    assert "✅" in frame["Status"].iloc[0]
    assert frame["Source"].iloc[0] == "builder"


# ===========================================================================
# Actions tab — purge + clear are gated by confirm checkboxes
# ===========================================================================


def test_purge_button_disabled_without_confirm_checkbox(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(submitted_at="2020-01-01T00:00:00Z")
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    # Operator clicks the button but does NOT check the confirm box.
    streamlit_recorder.button_responses["Purge now"] = True

    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    # The recorder's button(disabled=True) returns False, so no purge
    # fires when the confirm box is False.
    assert len(run_history.list_workflow_runs()) == 1


def test_purge_with_confirm_deletes_old_rows(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(run_id="r-ancient", submitted_at="2020-01-01T00:00:00Z")
    _seed_run(run_id="r-fresh", submitted_at="2999-01-01T00:00:00Z")
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    streamlit_recorder.widget_values[
        "I understand this is permanent"
    ] = True
    streamlit_recorder.widget_values[
        "Purge rows older than (days)"
    ] = 30
    streamlit_recorder.button_responses["Purge now"] = True

    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    remaining = {r.run_id for r in run_history.list_workflow_runs()}
    assert remaining == {"r-fresh"}


def test_clear_all_with_confirm_empties_db(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(run_id="r1")
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    streamlit_recorder.widget_values[
        "I really want to clear ALL local history"
    ] = True
    streamlit_recorder.button_responses["Clear all"] = True

    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    assert run_history.list_workflow_runs() == []


# ===========================================================================
# No connection — page still renders (falls back to all-partitions)
# ===========================================================================


def test_page_renders_without_connection_in_show_all_mode(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(run_id="r1", partition="some-partition")
    # No CONNECTION_KEY set.

    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    df_calls = streamlit_recorder.calls_named("dataframe")
    assert df_calls, "page should still render rows in show-all mode"
    page_links = streamlit_recorder.calls_named("page_link")
    assert page_links, "should link to Instance Configuration"


# ===========================================================================
# Locked session-state keys
# ===========================================================================


def test_locked_session_state_keys_initialized(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    locked = [
        page.HISTORY_SHOW_ALL_PARTITIONS_KEY,
        page.HISTORY_STATUS_FILTER_KEY,
        page.HISTORY_DATE_RANGE_KEY,
        page.HISTORY_LIMIT_KEY,
        page.HISTORY_UPLOADS_DATE_RANGE_KEY,
        page.HISTORY_UPLOADS_LIMIT_KEY,
        page.HISTORY_PURGE_DAYS_KEY,
        page.HISTORY_PURGE_CONFIRM_KEY,
        page.HISTORY_CLEAR_CONFIRM_KEY,
    ]
    for key in locked:
        assert key in streamlit_recorder.session_state, (
            f"locked key {key!r} not initialized"
        )


def test_uploads_tab_renders_dataframe_when_rows_exist(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_history.record_file_upload(
        record_id="opendes:dataset--File.Generic:abc",
        uploaded_at="2026-05-12T15:00:00Z",
        display_name="well.las",
        file_source="/staging/well",
        size_bytes=4096,
        data_partition_id="opendes",
    )
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    page = _load_page(streamlit_recorder, monkeypatch)
    page.main()

    # The dataframe call order depends on tab rendering; one of them
    # must contain the upload's display name.
    df_calls = streamlit_recorder.calls_named("dataframe")
    rendered = any(
        "well.las" in " ".join(
            str(v) for v in call.args[0]["Display name"].tolist()
        )
        for call in df_calls
        if "Display name" in call.args[0].columns
    )
    assert rendered
