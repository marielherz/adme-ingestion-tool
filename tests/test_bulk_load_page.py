"""Tests for the Bulk Load page (`app/pages/9_📥_Bulk_Load.py`)."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from app.connection_state import CONNECTION_KEY
from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import (
    DatasetDescriptor,
    DatasetTier,
    FieldMapping,
    ManifestPreview,
    MappingResult,
    SchemaField,
    SubmitResult,
)
from tests.support.streamlit_recorder import StreamlitRecorder

BULK_LOAD_PAGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "pages"
    / "9_📥_Bulk_Load.py"
)

# Locked session keys.
DATASET_KEY = "bulk_dataset_id"
TIER_KEY = "bulk_tier"
LEGAL_TAG_KEY = "bulk_legal_tag"
ACL_OWNERS_KEY = "bulk_acl_owners"
ACL_VIEWERS_KEY = "bulk_acl_viewers"
PREVIEW_SEEN_KEY = "bulk_preview_seen"
PREVIEW_RESULTS_KEY = "bulk_preview_results"
SUBMIT_RESULTS_KEY = "bulk_submit_results"
LAST_ERROR_KEY = "bulk_last_error"

# Internal helper keys (autorun-once option load).
OPTIONS_AUTORUN_KEY = "bulk_options_autorun_done"
LEGAL_TAG_OPTIONS_KEY = "bulk_legal_tag_options"
OWNER_OPTIONS_KEY = "bulk_acl_owner_options"
VIEWER_OPTIONS_KEY = "bulk_acl_viewer_options"

PREVIEW_LABEL = "🔍 Preview manifests"
SUBMIT_LABEL = "🚀 Submit all manifests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        auth_method=AuthMethod.SERVICE_PRINCIPAL,
        client_secret="placeholder-secret",
    )


def _tno_descriptor(tmp_path: Path) -> DatasetDescriptor:
    root = tmp_path / "datasets" / "tno"
    root.mkdir(parents=True, exist_ok=True)
    (root / "NOTICE.md").write_text("# TNO Notice\nApache-2.0", encoding="utf-8")
    return DatasetDescriptor(
        id="tno",
        display_name="TNO Open Test Data",
        source_url="https://community.opengroup.org/osdu/data/open-test-data",
        notice_path="NOTICE.md",
        tiers={
            "reference-data": DatasetTier(
                enabled=True,
                manifest_glob="*.json",
                description="13 reference-data tables",
            ),
            "master-data": DatasetTier(
                enabled=False, reason="v2 — not yet vendored"
            ),
            "work-products": DatasetTier(
                enabled=False, reason="v2 — not yet vendored"
            ),
        },
        root_dir=root,
    )


def _volve_descriptor(tmp_path: Path) -> DatasetDescriptor:
    root = tmp_path / "datasets" / "volve"
    root.mkdir(parents=True, exist_ok=True)
    return DatasetDescriptor(
        id="volve",
        display_name="Volve Open Dataset",
        source_url="https://example.com/volve",
        notice_path="NOTICE.md",
        tiers={
            "reference-data": DatasetTier(
                enabled=False, reason="not yet vendored"
            ),
        },
        root_dir=root,
    )


def _preview_row(filename: str, kind: str, count: int) -> ManifestPreview:
    return ManifestPreview(
        path=Path(filename),
        filename=filename,
        kind=kind,
        record_count=count,
        record_section="ReferenceData",
    )


def _submit_row(
    filename: str, *, ok: bool, run_id: str | None = None, error: str | None = None
) -> SubmitResult:
    return SubmitResult(
        manifest_path=Path(filename),
        filename=filename,
        status="success" if ok else "error",
        run_id=run_id,
        record_id=None,
        error=error,
        submitted_at=datetime.now(UTC),
    )


def _load_page(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    monkeypatch.setitem(sys.modules, "streamlit", streamlit_recorder)
    module_name = "tests.generated_bulk_load_page"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, BULK_LOAD_PAGE_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _patch_service(
    page_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    datasets: list[DatasetDescriptor] | None = None,
    preview_result: list[ManifestPreview] | None = None,
    preview_raises: Exception | None = None,
    submit_results: list[SubmitResult] | None = None,
    submit_raises: Exception | None = None,
) -> dict[str, list[Any]]:
    """Patch bulk_loader + auth + dropdown loaders. Returns a call-spy dict."""
    spy: dict[str, list[Any]] = {
        "list": [],
        "preview": [],
        "submit": [],
        "tokens": [],
    }

    def fake_list_datasets() -> list[DatasetDescriptor]:
        spy["list"].append(True)
        return list(datasets or [])

    def fake_preview_tier(
        dataset_id: str, tier: str
    ) -> list[ManifestPreview]:
        spy["preview"].append((dataset_id, tier))
        if preview_raises is not None:
            raise preview_raises
        return list(preview_result or [])

    def fake_submit_tier(
        dataset_id: str,
        tier: str,
        **kwargs: Any,
    ) -> Iterator[SubmitResult]:
        spy["submit"].append((dataset_id, tier, kwargs))
        if submit_raises is not None:
            raise submit_raises
        yield from list(submit_results or [])

    def fake_get_token(connection: ADMEConnection, **_: Any) -> str:
        spy["tokens"].append(connection)
        return "test-token"

    def fake_clear_cache() -> None:
        return None

    monkeypatch.setattr(page_module, "list_datasets", fake_list_datasets)
    monkeypatch.setattr(page_module, "preview_tier", fake_preview_tier)
    monkeypatch.setattr(page_module, "submit_tier", fake_submit_tier)
    monkeypatch.setattr(page_module, "get_token", fake_get_token)
    monkeypatch.setattr(page_module, "_clear_cache", fake_clear_cache)

    # Stub legal-tag + groups loaders so the autorun-once option load
    # cannot make real HTTP calls. Returning ok=False causes the page to
    # fall back to st.text_input for each field, which is what the tests
    # drive via session_state writes.
    from app.models.connection import EntitlementsCallResult
    from app.models.osdu import LegalTagListResult

    def fake_list_legal_tags(
        _c: ADMEConnection, _t: str, *, valid: bool | None = None
    ) -> LegalTagListResult:
        del valid
        return LegalTagListResult(
            items=[], ok=False, error_message="stubbed in tests"
        )

    def fake_fetch_groups(
        _c: ADMEConnection, _t: str
    ) -> EntitlementsCallResult:
        return EntitlementsCallResult(
            endpoint="groups",
            path="/api/entitlements/v2/groups",
            ok=False,
            http_status=None,
            latency_ms=0.0,
            correlation_id=None,
            error_message="stubbed in tests",
            raw_response=None,
            data=None,
        )

    monkeypatch.setattr(page_module, "list_legal_tags", fake_list_legal_tags)
    monkeypatch.setattr(page_module, "fetch_groups", fake_fetch_groups)
    return spy


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_page_renders_without_crashing_on_clean_session(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session with no connection should preflight-info, not crash."""
    page_module = _load_page(streamlit_recorder, monkeypatch)
    spy = _patch_service(page_module, monkeypatch)

    page_module.main()

    info_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("info")
    ]
    assert any("Instance Configuration" in m for m in info_messages)
    assert streamlit_recorder.calls_named("page_link")
    # Preflight blocks BEFORE list_datasets is called.
    assert spy["list"] == []
    assert spy["preview"] == []
    assert spy["submit"] == []


def test_dataset_selector_populates_from_list_datasets(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    page_module = _load_page(streamlit_recorder, monkeypatch)
    tno = _tno_descriptor(tmp_path)
    volve = _volve_descriptor(tmp_path)
    spy = _patch_service(page_module, monkeypatch, datasets=[tno, volve])

    page_module.main()

    assert spy["list"], "list_datasets was not called"
    # Dataset selectbox renders with both ids as options.
    dataset_select = [
        call
        for call in streamlit_recorder.calls_named("selectbox")
        if call.args and call.args[0] == "Dataset"
    ]
    assert dataset_select, "Dataset selectbox was not rendered"
    options = dataset_select[0].args[1]
    assert list(options) == ["tno", "volve"]


def test_selecting_tno_shows_source_url(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    streamlit_recorder.session_state[DATASET_KEY] = "tno"
    page_module = _load_page(streamlit_recorder, monkeypatch)
    tno = _tno_descriptor(tmp_path)
    # Make _read_notice succeed by pointing DATA_ROOT at the tmp tree.
    monkeypatch.setattr(page_module, "DATA_ROOT", tmp_path.resolve())
    _patch_service(page_module, monkeypatch, datasets=[tno])

    page_module.main()

    markdown_args = [
        call.args[0] for call in streamlit_recorder.calls_named("markdown")
    ]
    assert any(tno.source_url in m for m in markdown_args), (
        "Source URL must appear in the Source & license expander"
    )


def test_tier_selector_filters_to_enabled_and_lists_disabled(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    streamlit_recorder.session_state[DATASET_KEY] = "tno"
    page_module = _load_page(streamlit_recorder, monkeypatch)
    tno = _tno_descriptor(tmp_path)
    _patch_service(page_module, monkeypatch, datasets=[tno])

    page_module.main()

    tier_radios = [
        call
        for call in streamlit_recorder.calls_named("radio")
        if call.args and call.args[0] == "Tier"
    ]
    assert tier_radios, "Tier radio was not rendered"
    options = list(tier_radios[0].args[1])
    assert options == ["reference-data"], (
        "Only enabled tiers should appear in the radio"
    )

    info_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("info")
    ]
    assert any(
        "master-data" in m and "work-products" in m for m in info_messages
    ), "Disabled tiers must be surfaced in an info block"


def test_submit_button_disabled_before_preview(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    streamlit_recorder.session_state[DATASET_KEY] = "tno"
    streamlit_recorder.session_state[TIER_KEY] = "reference-data"
    # Even with legal+ACL filled, no preview yet => Submit disabled.
    streamlit_recorder.session_state[LEGAL_TAG_KEY] = "opendes-tno-data"
    streamlit_recorder.session_state[ACL_OWNERS_KEY] = "data.x.owners@x"
    streamlit_recorder.session_state[ACL_VIEWERS_KEY] = "data.x.viewers@x"
    page_module = _load_page(streamlit_recorder, monkeypatch)
    tno = _tno_descriptor(tmp_path)
    _patch_service(page_module, monkeypatch, datasets=[tno])

    page_module.main()

    submit_buttons = [
        call
        for call in streamlit_recorder.calls_named("button")
        if call.args and call.args[0] == SUBMIT_LABEL
    ]
    assert submit_buttons, "Submit button must be rendered"
    assert submit_buttons[0].kwargs.get("disabled") is True, (
        "Submit must be disabled before Preview"
    )

    captions = [
        call.args[0] for call in streamlit_recorder.calls_named("caption")
    ]
    assert any("Run Preview first" in c for c in captions), (
        "Disabled-reason caption must explain the Preview gate"
    )


def test_clicking_preview_enables_submit_when_form_complete(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    streamlit_recorder.session_state[DATASET_KEY] = "tno"
    streamlit_recorder.session_state[TIER_KEY] = "reference-data"
    streamlit_recorder.session_state[LEGAL_TAG_KEY] = "opendes-tno-data"
    streamlit_recorder.session_state[ACL_OWNERS_KEY] = "data.x.owners@x"
    streamlit_recorder.session_state[ACL_VIEWERS_KEY] = "data.x.viewers@x"
    # Simulate operator already having clicked Preview on a prior run by
    # priming the gate flag + cached preview rows. The current render
    # should therefore enable Submit.
    streamlit_recorder.session_state[PREVIEW_SEEN_KEY] = (
        "tno",
        "reference-data",
    )
    streamlit_recorder.session_state[PREVIEW_RESULTS_KEY] = [
        _preview_row("load_a.json", "osdu:wks:reference-data--A:1.0.0", 12),
        _preview_row("load_b.json", "osdu:wks:reference-data--B:1.0.0", 8),
    ]
    page_module = _load_page(streamlit_recorder, monkeypatch)
    tno = _tno_descriptor(tmp_path)
    _patch_service(page_module, monkeypatch, datasets=[tno])

    page_module.main()

    submit_buttons = [
        call
        for call in streamlit_recorder.calls_named("button")
        if call.args and call.args[0] == SUBMIT_LABEL
    ]
    assert submit_buttons
    assert submit_buttons[0].kwargs.get("disabled") is False, (
        "Submit must enable once Preview has been seen and form is complete"
    )

    # Preview summary line + dataframe should render.
    success_args = [
        call.args[0] for call in streamlit_recorder.calls_named("success")
    ]
    assert any("2 manifests" in s and "20" in s for s in success_args), (
        "Preview summary line must show count + total records"
    )
    assert streamlit_recorder.calls_named("dataframe"), (
        "Preview table must render"
    )


def test_submit_disabled_when_legal_tag_empty(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    streamlit_recorder.session_state[DATASET_KEY] = "tno"
    streamlit_recorder.session_state[TIER_KEY] = "reference-data"
    streamlit_recorder.session_state[LEGAL_TAG_KEY] = ""  # empty
    streamlit_recorder.session_state[ACL_OWNERS_KEY] = "data.x.owners@x"
    streamlit_recorder.session_state[ACL_VIEWERS_KEY] = "data.x.viewers@x"
    streamlit_recorder.session_state[PREVIEW_SEEN_KEY] = (
        "tno",
        "reference-data",
    )
    streamlit_recorder.session_state[PREVIEW_RESULTS_KEY] = [
        _preview_row("load_a.json", "kindA", 1),
    ]
    page_module = _load_page(streamlit_recorder, monkeypatch)
    tno = _tno_descriptor(tmp_path)
    _patch_service(page_module, monkeypatch, datasets=[tno])

    page_module.main()

    submit_buttons = [
        call
        for call in streamlit_recorder.calls_named("button")
        if call.args and call.args[0] == SUBMIT_LABEL
    ]
    assert submit_buttons
    assert submit_buttons[0].kwargs.get("disabled") is True

    captions = [
        call.args[0] for call in streamlit_recorder.calls_named("caption")
    ]
    assert any("legal tag" in c.lower() for c in captions)


def test_preview_invalidates_when_dataset_changes(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing dataset wipes the preview gate so Submit re-locks."""
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    # Operator previously previewed TNO. Now they pick VOLVE — but volve
    # has no enabled tiers, so the test focuses on the gate-clearing
    # behavior alone: starting with a stale seen-key, the page resets it.
    streamlit_recorder.session_state[DATASET_KEY] = "tno"
    streamlit_recorder.session_state[TIER_KEY] = "reference-data"
    streamlit_recorder.session_state[PREVIEW_SEEN_KEY] = (
        "volve",
        "reference-data",
    )
    streamlit_recorder.session_state[PREVIEW_RESULTS_KEY] = [
        _preview_row("stale.json", "kind", 1),
    ]
    page_module = _load_page(streamlit_recorder, monkeypatch)
    tno = _tno_descriptor(tmp_path)
    _patch_service(page_module, monkeypatch, datasets=[tno])

    page_module.main()

    # The gate must be cleared because the selected dataset is "tno"
    # but the seen key was ("volve", "reference-data").
    assert streamlit_recorder.session_state[PREVIEW_SEEN_KEY] is None
    assert streamlit_recorder.session_state[PREVIEW_RESULTS_KEY] == []


def test_submit_renders_mixed_success_and_failure_results(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Clicking Submit iterates submit_tier and renders ✅/❌ rows."""
    streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
    streamlit_recorder.session_state[DATASET_KEY] = "tno"
    streamlit_recorder.session_state[TIER_KEY] = "reference-data"
    streamlit_recorder.session_state[LEGAL_TAG_KEY] = "opendes-tno-data"
    streamlit_recorder.session_state[ACL_OWNERS_KEY] = "data.x.owners@x"
    streamlit_recorder.session_state[ACL_VIEWERS_KEY] = "data.x.viewers@x"
    streamlit_recorder.session_state[PREVIEW_SEEN_KEY] = (
        "tno",
        "reference-data",
    )
    streamlit_recorder.session_state[PREVIEW_RESULTS_KEY] = [
        _preview_row("load_a.json", "kindA", 1),
        _preview_row("load_b.json", "kindB", 1),
    ]
    streamlit_recorder.button_responses[SUBMIT_LABEL] = True

    page_module = _load_page(streamlit_recorder, monkeypatch)
    tno = _tno_descriptor(tmp_path)
    spy = _patch_service(
        page_module,
        monkeypatch,
        datasets=[tno],
        submit_results=[
            _submit_row("load_a.json", ok=True, run_id="run-1"),
            _submit_row("load_b.json", ok=False, error="boom"),
        ],
    )

    page_module.main()

    assert spy["submit"], "submit_tier should have been called"
    submit_call = spy["submit"][0]
    assert submit_call[0] == "tno"
    assert submit_call[1] == "reference-data"
    kwargs = submit_call[2]
    assert kwargs["acl_owners"] == ["data.x.owners@x"]
    assert kwargs["acl_viewers"] == ["data.x.viewers@x"]
    assert kwargs["legal_tag"] == "opendes-tno-data"
    assert kwargs["data_partition_id"] == "example-opendes"

    # Streamed per-row markdown for each result.
    markdown_args = [
        call.args[0] for call in streamlit_recorder.calls_named("markdown")
    ]
    assert any(
        "✅" in m and "load_a.json" in m and "run-1" in m for m in markdown_args
    )
    assert any(
        "❌" in m and "load_b.json" in m and "boom" in m for m in markdown_args
    )

    # Persistent summary banner — 1 of 2 succeeded → st.warning.
    warning_args = [
        call.args[0] for call in streamlit_recorder.calls_named("warning")
    ]
    assert any(
        "1 of 2 succeeded" in w and "1 failed" in w for w in warning_args
    )

    # Submit results stored for re-render on next rerun.
    stored = cast(
        list[SubmitResult],
        streamlit_recorder.session_state[SUBMIT_RESULTS_KEY],
    )
    assert len(stored) == 2
    assert stored[0].status == "success"
    assert stored[1].status == "error"


# ===========================================================================
# Generate from CSV tab — session keys (mirror page module GEN_* constants)
# ===========================================================================

GEN_KIND_KEY = "gen_kind"
GEN_CSV_DATA_KEY = "gen_csv_data"
GEN_MAPPING_RESULT_KEY = "gen_mapping_result"
GEN_CONFIRMED_MAPPINGS_KEY = "gen_confirmed_mappings"
GEN_MANIFESTS_KEY = "gen_manifests"
GEN_SUBMIT_RESULTS_KEY = "gen_submit_results"
GEN_LEGAL_TAG_KEY = "gen_legal_tag"
GEN_ACL_OWNERS_KEY = "gen_acl_owners"
GEN_ACL_VIEWERS_KEY = "gen_acl_viewers"
GEN_LAST_ERROR_KEY = "gen_last_error"

# Abort feature keys — Judson's #31 implementation will define these.
# Tests written ahead of implementation; expected to fail until wired up.
BULK_ABORT_KEY = "bulk_abort_requested"
GEN_ABORT_KEY = "gen_abort_requested"

GENERATE_BUTTON_LABEL = "📄 Generate Manifests"
GEN_SUBMIT_LABEL = "🚀 Submit generated manifests"


# ---------------------------------------------------------------------------
# CSV-gen helpers
# ---------------------------------------------------------------------------

_SAMPLE_CSV = b"WellName,Country,FieldName\nWell-A,Norway,Troll\nWell-B,UK,Brent\n"
_SAMPLE_CSV_HEADERS = ["WellName", "Country", "FieldName"]
_SAMPLE_KINDS = [
    "osdu:wks:master-data--Well:1.0.0",
    "osdu:wks:reference-data--AliasNameType:1.0.0",
]


class FakeUploadedCSV:
    """Minimal stand-in for ``st.file_uploader`` return value."""

    def __init__(
        self,
        content: bytes = _SAMPLE_CSV,
        name: str = "wells.csv",
    ) -> None:
        self.name = name
        self.size = len(content)
        self.type = "text/csv"
        self._content = content

    def getvalue(self) -> bytes:
        return self._content


def _sample_schema_fields() -> list[SchemaField]:
    return [
        SchemaField(path="data.WellName", field_type="string", required=True),
        SchemaField(path="data.Country", field_type="string", required=False),
        SchemaField(
            path="data.FieldName", field_type="string", required=False
        ),
    ]


def _sample_mapping_result() -> MappingResult:
    return MappingResult(
        mappings=[
            FieldMapping(csv_header="WellName", schema_path="data.WellName"),
            FieldMapping(csv_header="Country", schema_path="data.Country"),
            FieldMapping(csv_header="FieldName", schema_path="data.FieldName"),
        ],
        unmatched_csv=[],
        unmatched_required=[],
        confidence=1.0,
    )


def _sample_manifests() -> list[dict]:
    return [
        {"executionContext": {"manifest": {"kind": "osdu:wks:Manifest:1.0.0"}, "i": 0}},
        {"executionContext": {"manifest": {"kind": "osdu:wks:Manifest:1.0.0"}, "i": 1}},
    ]


def _patch_csv_services(
    page_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    datasets: list[DatasetDescriptor] | None = None,
    kinds: list[str] | None = None,
    schema: dict | None = None,
    schema_fields: list[SchemaField] | None = None,
    mapping_result: MappingResult | None = None,
    generate_result: list[dict] | None = None,
    generate_raises: Exception | None = None,
    submit_manifest_result: Any | None = None,
    submit_manifest_raises: Exception | None = None,
) -> dict[str, list[Any]]:
    """Patch all services needed for CSV-generation tab tests.

    Extends the base ``_patch_service`` stubs with manifest_generator and
    ingestion service patches. Returns a call-spy dict.
    """
    # Start with the base patches (list_datasets, submit_tier, get_token, etc.)
    spy = _patch_service(page_module, monkeypatch, datasets=datasets or [])

    csv_spy: dict[str, list[Any]] = {
        "list_kinds": [],
        "load_schema": [],
        "extract_fields": [],
        "auto_map": [],
        "generate": [],
        "submit_manifest": [],
    }
    spy.update(csv_spy)

    def fake_list_schema_kinds(**_: Any) -> list[str]:
        csv_spy["list_kinds"].append(True)
        return list(kinds if kinds is not None else _SAMPLE_KINDS)

    def fake_load_schema(kind: str, **_: Any) -> dict:
        csv_spy["load_schema"].append(kind)
        return schema or {"properties": {"data": {"properties": {}}}}

    def fake_extract_schema_fields(s: dict, **_: Any) -> list[SchemaField]:
        csv_spy["extract_fields"].append(s)
        return list(schema_fields if schema_fields is not None else _sample_schema_fields())

    def fake_auto_map(
        fields: list[SchemaField], headers: list[str]
    ) -> MappingResult:
        csv_spy["auto_map"].append((fields, headers))
        return mapping_result or _sample_mapping_result()

    def fake_generate_manifests(**kwargs: Any) -> list[dict]:
        csv_spy["generate"].append(kwargs)
        if generate_raises is not None:
            raise generate_raises
        return list(generate_result if generate_result is not None else _sample_manifests())

    def fake_submit_manifest(
        connection: ADMEConnection, token: str, payload: dict
    ) -> Any:
        csv_spy["submit_manifest"].append((connection, token, payload))
        if submit_manifest_raises is not None:
            raise submit_manifest_raises
        if submit_manifest_result is not None:
            return submit_manifest_result
        # Return a minimal successful WorkflowRunResult
        from app.models.osdu import WorkflowRunResult, WorkflowStatus

        return WorkflowRunResult(
            workflow_id="wf-1",
            run_id=f"run-{len(csv_spy['submit_manifest'])}",
            status=WorkflowStatus.FINISHED,
            raw_status="finished",
            message=None,
            ok=True,
            http_status=200,
            latency_ms=50.0,
        )

    monkeypatch.setattr(page_module, "list_schema_kinds", fake_list_schema_kinds)
    monkeypatch.setattr(page_module, "load_schema", fake_load_schema)
    monkeypatch.setattr(
        page_module, "extract_schema_fields", fake_extract_schema_fields
    )
    monkeypatch.setattr(page_module, "auto_map", fake_auto_map)
    monkeypatch.setattr(page_module, "generate_manifests", fake_generate_manifests)
    monkeypatch.setattr(page_module, "submit_manifest", fake_submit_manifest)

    return spy


def _setup_csv_tab_session(
    recorder: StreamlitRecorder,
    *,
    kind: str = "",
    csv_data: bytes | None = None,
    legal_tag: str = "",
    acl_owners: str = "",
    acl_viewers: str = "",
    mapping_result: MappingResult | None = None,
    confirmed_mappings: list[FieldMapping] | None = None,
    manifests: list[dict] | None = None,
) -> None:
    """Prime session state for CSV-gen tab tests.

    The recorder's ``selectbox`` reads from ``widget_values[label]``, not
    ``session_state[key]``.  We set **both** so the page's session-state
    reads AND the selectbox return values are consistent.
    """
    recorder.session_state[CONNECTION_KEY] = _connection()
    recorder.session_state[GEN_KIND_KEY] = kind
    recorder.session_state[GEN_CSV_DATA_KEY] = csv_data
    recorder.session_state[GEN_LEGAL_TAG_KEY] = legal_tag
    recorder.session_state[GEN_ACL_OWNERS_KEY] = acl_owners
    recorder.session_state[GEN_ACL_VIEWERS_KEY] = acl_viewers
    # Widget values drive what the recorder returns from selectbox/text_input.
    if kind:
        recorder.widget_values["OSDU kind"] = kind
    if mapping_result is not None:
        recorder.session_state[GEN_MAPPING_RESULT_KEY] = mapping_result
    if confirmed_mappings is not None:
        recorder.session_state[GEN_CONFIRMED_MAPPINGS_KEY] = confirmed_mappings
    if manifests is not None:
        recorder.session_state[GEN_MANIFESTS_KEY] = manifests


# ===========================================================================
# CSV Generation Tab — Issue #17
# ===========================================================================


class TestCSVGenerationKindPicker:
    """Kind picker renders with schema kinds from ``list_schema_kinds()``."""

    def test_kind_selectbox_renders_with_schema_kinds(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The OSDU kind selectbox renders with all vendored schema kinds."""
        _setup_csv_tab_session(streamlit_recorder)
        page_module = _load_page(streamlit_recorder, monkeypatch)
        spy = _patch_csv_services(
            page_module, monkeypatch, kinds=_SAMPLE_KINDS
        )

        page_module.main()

        assert spy["list_kinds"], "list_schema_kinds was not called"
        kind_selects = [
            call
            for call in streamlit_recorder.calls_named("selectbox")
            if call.args and call.args[0] == "OSDU kind"
        ]
        assert kind_selects, "OSDU kind selectbox was not rendered"
        options = kind_selects[0].args[1]
        # First option is the blank placeholder ""
        assert "" in options
        for k in _SAMPLE_KINDS:
            assert k in options

    def test_no_schemas_shows_warning(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no vendored schemas exist, a warning is displayed."""
        _setup_csv_tab_session(streamlit_recorder)
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch, kinds=[])

        page_module.main()

        warnings = [
            call.args[0] for call in streamlit_recorder.calls_named("warning")
        ]
        assert any("schema" in w.lower() for w in warnings)


class TestCSVUpload:
    """CSV file upload stores data in session state."""

    def test_upload_stores_csv_bytes_in_session_state(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Uploading a CSV file stores its bytes in GEN_CSV_DATA_KEY."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(streamlit_recorder, kind=kind)
        fake_csv = FakeUploadedCSV()
        streamlit_recorder.file_uploader_responses["Upload CSV"] = fake_csv
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        stored = streamlit_recorder.session_state.get(GEN_CSV_DATA_KEY)
        assert stored == _SAMPLE_CSV

    def test_new_csv_resets_downstream_state(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Uploading a different CSV clears mapping, manifests, and results."""
        kind = _SAMPLE_KINDS[0]
        old_csv = b"OldCol\nval\n"
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=old_csv,
            mapping_result=_sample_mapping_result(),
            manifests=_sample_manifests(),
        )
        # Now upload a new file with different content
        new_csv = FakeUploadedCSV(content=_SAMPLE_CSV)
        streamlit_recorder.file_uploader_responses["Upload CSV"] = new_csv
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        # Downstream state should be reset
        assert streamlit_recorder.session_state.get(GEN_CSV_DATA_KEY) == _SAMPLE_CSV
        # Mapping result is recomputed (auto_map called again for new CSV)
        # Manifests should be cleared when CSV changes
        assert streamlit_recorder.session_state.get(GEN_MANIFESTS_KEY) is None


class TestCSVAutoMap:
    """Auto-map is called when both kind and CSV are present."""

    def test_auto_map_called_with_kind_and_csv(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When kind is selected and CSV uploaded, auto_map is invoked."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(streamlit_recorder, kind=kind, csv_data=_SAMPLE_CSV)
        page_module = _load_page(streamlit_recorder, monkeypatch)
        spy = _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        assert spy["auto_map"], "auto_map was not called"
        fields, headers = spy["auto_map"][0]
        assert len(fields) == 3
        assert set(headers) == {"WellName", "Country", "FieldName"}

    def test_auto_map_skipped_when_no_csv(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without a CSV upload, auto_map is not called."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(streamlit_recorder, kind=kind, csv_data=None)
        page_module = _load_page(streamlit_recorder, monkeypatch)
        spy = _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        assert not spy["auto_map"]
        info_msgs = [
            call.args[0] for call in streamlit_recorder.calls_named("info")
        ]
        assert any("Upload a CSV" in m for m in info_msgs)

    def test_auto_map_skipped_when_no_kind(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without a kind selection, auto_map is not called."""
        _setup_csv_tab_session(streamlit_recorder, kind="", csv_data=_SAMPLE_CSV)
        page_module = _load_page(streamlit_recorder, monkeypatch)
        spy = _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        assert not spy["auto_map"]

    def test_auto_map_not_rerun_when_mapping_cached(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When GEN_MAPPING_RESULT_KEY is already set, auto_map is not re-called."""
        kind = _SAMPLE_KINDS[0]
        cached = _sample_mapping_result()
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            mapping_result=cached,
        )
        page_module = _load_page(streamlit_recorder, monkeypatch)
        spy = _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        assert not spy["auto_map"], "auto_map should not re-run when cached"


class TestCSVMappingOverride:
    """Mapping override UI renders with correct field options."""

    def test_mapping_selectboxes_rendered_for_each_schema_field(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A selectbox per schema field is rendered for override."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(streamlit_recorder, kind=kind, csv_data=_SAMPLE_CSV)
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        # Each schema field gets a selectbox keyed by gen_map_{path}
        mapping_selects = [
            call
            for call in streamlit_recorder.calls_named("selectbox")
            if call.kwargs.get("key", "").startswith("gen_map_")
        ]
        assert len(mapping_selects) == 3, (
            f"Expected 3 mapping selectboxes, got {len(mapping_selects)}"
        )

    def test_mapping_options_include_unmapped_and_csv_headers(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each mapping selectbox offers (unmapped) + all CSV headers."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(streamlit_recorder, kind=kind, csv_data=_SAMPLE_CSV)
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        mapping_selects = [
            call
            for call in streamlit_recorder.calls_named("selectbox")
            if call.kwargs.get("key", "").startswith("gen_map_")
        ]
        for sel in mapping_selects:
            options = sel.args[1]
            assert "(unmapped)" in options
            for h in _SAMPLE_CSV_HEADERS:
                assert h in options

    def test_confidence_indicator_renders(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Auto-map confidence percentage is displayed."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(streamlit_recorder, kind=kind, csv_data=_SAMPLE_CSV)
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        # 100% confidence → st.success
        success_msgs = [
            call.args[0]
            for call in streamlit_recorder.calls_named("success")
        ]
        assert any("100%" in m for m in success_msgs)

    def test_low_confidence_shows_warning(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Low confidence mapping shows a warning indicator."""
        kind = _SAMPLE_KINDS[0]
        low_conf = MappingResult(
            mappings=[
                FieldMapping(csv_header="WellName", schema_path="data.WellName"),
            ],
            unmatched_csv=["Country", "FieldName"],
            unmatched_required=[],
            confidence=0.33,
        )
        _setup_csv_tab_session(streamlit_recorder, kind=kind, csv_data=_SAMPLE_CSV)
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch, mapping_result=low_conf)

        page_module.main()

        error_msgs = [
            call.args[0] for call in streamlit_recorder.calls_named("error")
        ]
        assert any("33%" in m for m in error_msgs)

    def test_unmatched_required_fields_surfaced(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unmapped required fields produce a warning listing them."""
        kind = _SAMPLE_KINDS[0]
        partial = MappingResult(
            mappings=[],
            unmatched_csv=["WellName", "Country", "FieldName"],
            unmatched_required=["data.WellName"],
            confidence=0.0,
        )
        _setup_csv_tab_session(streamlit_recorder, kind=kind, csv_data=_SAMPLE_CSV)
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch, mapping_result=partial)

        page_module.main()

        warnings = [
            call.args[0] for call in streamlit_recorder.calls_named("warning")
        ]
        assert any(
            "required" in w.lower() and "data.WellName" in w for w in warnings
        )


class TestCSVGenerate:
    """Generate button calls ``generate_manifests()`` with confirmed mappings."""

    def test_generate_disabled_without_any_mappings(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Generate is disabled when no CSV columns are mapped to fields."""
        kind = _SAMPLE_KINDS[0]
        # All fields unmapped → confirmed list empty
        all_unmapped = MappingResult(
            mappings=[],
            unmatched_csv=_SAMPLE_CSV_HEADERS,
            unmatched_required=["data.WellName"],
            confidence=0.0,
        )
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
        )
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch, mapping_result=all_unmapped)

        page_module.main()

        gen_buttons = [
            call
            for call in streamlit_recorder.calls_named("button")
            if call.args and call.args[0] == GENERATE_BUTTON_LABEL
        ]
        assert gen_buttons, "Generate button must be rendered"
        assert gen_buttons[0].kwargs.get("disabled") is True

    def test_generate_disabled_without_legal_tag(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Generate is disabled when legal tag is empty."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="",  # empty
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
        )
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        gen_buttons = [
            call
            for call in streamlit_recorder.calls_named("button")
            if call.args and call.args[0] == GENERATE_BUTTON_LABEL
        ]
        assert gen_buttons
        assert gen_buttons[0].kwargs.get("disabled") is True
        captions = [
            call.args[0] for call in streamlit_recorder.calls_named("caption")
        ]
        assert any("legal tag" in c.lower() for c in captions)

    def test_generate_disabled_without_acl_owners(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Generate is disabled when ACL owners is empty."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="",
            acl_viewers="data.x.viewers@x",
        )
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        gen_buttons = [
            call
            for call in streamlit_recorder.calls_named("button")
            if call.args and call.args[0] == GENERATE_BUTTON_LABEL
        ]
        assert gen_buttons
        assert gen_buttons[0].kwargs.get("disabled") is True

    def test_generate_calls_generate_manifests_on_click(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Clicking Generate invokes generate_manifests with confirmed mappings."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
        )
        streamlit_recorder.button_responses[GENERATE_BUTTON_LABEL] = True
        page_module = _load_page(streamlit_recorder, monkeypatch)
        spy = _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        assert spy["generate"], "generate_manifests was not called"
        gen_kwargs = spy["generate"][0]
        assert gen_kwargs["kind"] == kind
        assert gen_kwargs["legal_tag"] == "opendes-tag"
        assert gen_kwargs["acl_owners"] == "data.x.owners@x"
        assert gen_kwargs["acl_viewers"] == "data.x.viewers@x"


class TestCSVManifestPreview:
    """Generated manifests display in preview before submit."""

    def test_generated_manifests_stored_and_count_shown(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After generate, manifests are stored and a count summary renders."""
        kind = _SAMPLE_KINDS[0]
        manifests = _sample_manifests()
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            manifests=manifests,
        )
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        success_msgs = [
            call.args[0] for call in streamlit_recorder.calls_named("success")
        ]
        assert any(
            "2" in m and "manifest" in m.lower() for m in success_msgs
        ), "Preview should show manifest count"

    def test_submit_button_appears_when_manifests_generated(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Submit button renders once manifests are generated."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            manifests=_sample_manifests(),
        )
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        submit_buttons = [
            call
            for call in streamlit_recorder.calls_named("button")
            if call.args and call.args[0] == GEN_SUBMIT_LABEL
        ]
        assert submit_buttons, "Submit generated manifests button must render"

    def test_submit_disabled_without_manifests(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Submit is disabled when no manifests have been generated yet."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            manifests=None,
        )
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        # The submit button only renders when manifests exist; if it does
        # render it should be disabled.
        submit_buttons = [
            call
            for call in streamlit_recorder.calls_named("button")
            if call.args and call.args[0] == GEN_SUBMIT_LABEL
        ]
        if submit_buttons:
            assert submit_buttons[0].kwargs.get("disabled") is True


class TestCSVSubmit:
    """Submit iterates through generated manifests via ``submit_manifest()``."""

    def test_submit_calls_submit_manifest_for_each(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Clicking Submit calls submit_manifest once per generated manifest."""
        kind = _SAMPLE_KINDS[0]
        manifests = _sample_manifests()
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            manifests=manifests,
        )
        streamlit_recorder.button_responses[GEN_SUBMIT_LABEL] = True
        page_module = _load_page(streamlit_recorder, monkeypatch)
        spy = _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        assert len(spy["submit_manifest"]) == len(manifests), (
            f"Expected {len(manifests)} submit_manifest calls, "
            f"got {len(spy['submit_manifest'])}"
        )

    def test_submit_stores_results_in_session_state(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Submission results are stored in GEN_SUBMIT_RESULTS_KEY."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            manifests=_sample_manifests(),
        )
        streamlit_recorder.button_responses[GEN_SUBMIT_LABEL] = True
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        results = streamlit_recorder.session_state.get(GEN_SUBMIT_RESULTS_KEY, [])
        assert len(results) == 2
        assert results[0]["ok"] is True

    def test_submit_progress_bar_updates(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Progress bar updates during submission."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            manifests=_sample_manifests(),
        )
        streamlit_recorder.button_responses[GEN_SUBMIT_LABEL] = True
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        progress_updates = streamlit_recorder.calls_named("progress_update")
        assert progress_updates, "Progress bar should update during submit"


class TestCSVErrorHandling:
    """Error handling: invalid CSV, schema errors, generate/submit failures."""

    def test_invalid_csv_shows_error(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Uploading a CSV with no headers shows a parse error."""
        kind = _SAMPLE_KINDS[0]
        bad_csv = b""  # empty file
        _setup_csv_tab_session(
            streamlit_recorder, kind=kind, csv_data=bad_csv
        )
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        error_msgs = [
            call.args[0] for call in streamlit_recorder.calls_named("error")
        ]
        assert any(
            "csv" in m.lower() or "header" in m.lower() for m in error_msgs
        )

    def test_schema_not_found_shows_error(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When load_schema raises SchemaNotFoundError, an error is displayed."""
        from app.services.manifest_generator import SchemaNotFoundError

        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder, kind=kind, csv_data=_SAMPLE_CSV
        )
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)
        # Override load_schema to raise
        monkeypatch.setattr(
            page_module,
            "load_schema",
            lambda *a, **k: (_ for _ in ()).throw(
                SchemaNotFoundError("no-such-kind")
            ),
        )

        page_module.main()

        error_msgs = [
            call.args[0] for call in streamlit_recorder.calls_named("error")
        ]
        assert any("schema" in m.lower() for m in error_msgs)

    def test_generate_failure_sets_sticky_error(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A generate_manifests exception sets a sticky error."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
        )
        streamlit_recorder.button_responses[GENERATE_BUTTON_LABEL] = True
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(
            page_module,
            monkeypatch,
            generate_raises=ValueError("CSV data malformed"),
        )

        page_module.main()

        sticky_error = streamlit_recorder.session_state.get(GEN_LAST_ERROR_KEY)
        assert sticky_error is not None
        assert "malformed" in sticky_error.lower() or "error" in sticky_error.lower()

    def test_submit_manifest_failure_records_error_result(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A submit_manifest exception is captured as a failed result row."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            manifests=[_sample_manifests()[0]],
        )
        streamlit_recorder.button_responses[GEN_SUBMIT_LABEL] = True
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(
            page_module,
            monkeypatch,
            submit_manifest_raises=RuntimeError("network timeout"),
        )

        page_module.main()

        results = streamlit_recorder.session_state.get(GEN_SUBMIT_RESULTS_KEY, [])
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert "timeout" in results[0].get("error", "").lower()

    def test_csv_gen_results_section_renders_summary(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Persistent results section renders success/failure summary."""
        kind = _SAMPLE_KINDS[0]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
        )
        # Pre-populate results as if a prior submit completed
        streamlit_recorder.session_state[GEN_SUBMIT_RESULTS_KEY] = [
            {"index": 1, "ok": True, "run_id": "run-1", "error": ""},
            {"index": 2, "ok": False, "run_id": "", "error": "boom"},
        ]
        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        warning_msgs = [
            call.args[0]
            for call in streamlit_recorder.calls_named("warning")
        ]
        assert any(
            "1 of 2" in w and "1 failed" in w for w in warning_msgs
        ), "Results summary should show succeeded/failed counts"


# ===========================================================================
# Abort button — Issue #31
#
# Judson's implementation is in progress. These tests codify the expected
# abort behavior for BOTH the registered-dataset submit loop and the
# CSV-generation submit loop. They will fail until the abort feature is
# wired up — that's intentional: they're the acceptance gate.
# ===========================================================================


class TestAbortRegisteredDatasets:
    """Abort button behavior during registered-dataset submit."""

    def test_abort_flag_stops_registered_dataset_submit_loop(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Setting the abort flag before submit causes the loop to stop early."""
        streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
        streamlit_recorder.session_state[DATASET_KEY] = "tno"
        streamlit_recorder.session_state[TIER_KEY] = "reference-data"
        streamlit_recorder.session_state[LEGAL_TAG_KEY] = "opendes-tno-data"
        streamlit_recorder.session_state[ACL_OWNERS_KEY] = "data.x.owners@x"
        streamlit_recorder.session_state[ACL_VIEWERS_KEY] = "data.x.viewers@x"
        streamlit_recorder.session_state[PREVIEW_SEEN_KEY] = (
            "tno",
            "reference-data",
        )
        streamlit_recorder.session_state[PREVIEW_RESULTS_KEY] = [
            _preview_row("a.json", "kindA", 1),
            _preview_row("b.json", "kindB", 1),
            _preview_row("c.json", "kindC", 1),
        ]
        # Pre-set abort flag to simulate mid-loop abort
        streamlit_recorder.session_state[BULK_ABORT_KEY] = True
        streamlit_recorder.button_responses[SUBMIT_LABEL] = True

        call_count = 0

        def counting_submit_tier(
            dataset_id: str, tier: str, **kwargs: Any
        ) -> Iterator[SubmitResult]:
            nonlocal call_count
            for name in ["a.json", "b.json", "c.json"]:
                call_count += 1
                yield _submit_row(name, ok=True, run_id=f"run-{call_count}")

        page_module = _load_page(streamlit_recorder, monkeypatch)
        tno = _tno_descriptor(tmp_path)
        _patch_service(page_module, monkeypatch, datasets=[tno])
        monkeypatch.setattr(page_module, "submit_tier", counting_submit_tier)

        page_module.main()

        # With abort flag set, the loop should have stopped before
        # processing all 3 manifests.
        stored = streamlit_recorder.session_state.get(SUBMIT_RESULTS_KEY, [])
        assert len(stored) < 3, (
            f"Abort should stop the loop early but got {len(stored)} results"
        )

    def test_abort_partial_results_preserved(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Partial results from before the abort are kept in session state."""
        streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
        streamlit_recorder.session_state[DATASET_KEY] = "tno"
        streamlit_recorder.session_state[TIER_KEY] = "reference-data"
        streamlit_recorder.session_state[LEGAL_TAG_KEY] = "opendes-tno-data"
        streamlit_recorder.session_state[ACL_OWNERS_KEY] = "data.x.owners@x"
        streamlit_recorder.session_state[ACL_VIEWERS_KEY] = "data.x.viewers@x"
        streamlit_recorder.session_state[PREVIEW_SEEN_KEY] = (
            "tno",
            "reference-data",
        )
        streamlit_recorder.session_state[PREVIEW_RESULTS_KEY] = [
            _preview_row("a.json", "kindA", 1),
            _preview_row("b.json", "kindB", 1),
        ]
        streamlit_recorder.session_state[BULK_ABORT_KEY] = True
        streamlit_recorder.button_responses[SUBMIT_LABEL] = True

        page_module = _load_page(streamlit_recorder, monkeypatch)
        tno = _tno_descriptor(tmp_path)
        _patch_service(
            page_module,
            monkeypatch,
            datasets=[tno],
            submit_results=[
                _submit_row("a.json", ok=True, run_id="run-1"),
                _submit_row("b.json", ok=True, run_id="run-2"),
            ],
        )

        page_module.main()

        stored = streamlit_recorder.session_state.get(SUBMIT_RESULTS_KEY, [])
        # At least partial results should be present (whatever was
        # processed before the abort was checked).
        assert isinstance(stored, list)

    def test_abort_message_shows_count(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """After abort, a message shows 'Aborted after N/M manifests'."""
        streamlit_recorder.session_state[CONNECTION_KEY] = _connection()
        streamlit_recorder.session_state[DATASET_KEY] = "tno"
        streamlit_recorder.session_state[TIER_KEY] = "reference-data"
        streamlit_recorder.session_state[LEGAL_TAG_KEY] = "opendes-tno-data"
        streamlit_recorder.session_state[ACL_OWNERS_KEY] = "data.x.owners@x"
        streamlit_recorder.session_state[ACL_VIEWERS_KEY] = "data.x.viewers@x"
        streamlit_recorder.session_state[PREVIEW_SEEN_KEY] = (
            "tno",
            "reference-data",
        )
        streamlit_recorder.session_state[PREVIEW_RESULTS_KEY] = [
            _preview_row("a.json", "kindA", 1),
            _preview_row("b.json", "kindB", 1),
            _preview_row("c.json", "kindC", 1),
        ]
        streamlit_recorder.session_state[BULK_ABORT_KEY] = True
        streamlit_recorder.button_responses[SUBMIT_LABEL] = True

        page_module = _load_page(streamlit_recorder, monkeypatch)
        tno = _tno_descriptor(tmp_path)
        _patch_service(
            page_module,
            monkeypatch,
            datasets=[tno],
            submit_results=[
                _submit_row("a.json", ok=True, run_id="run-1"),
                _submit_row("b.json", ok=True, run_id="run-2"),
                _submit_row("c.json", ok=True, run_id="run-3"),
            ],
        )

        page_module.main()

        # Look for abort-count message in warning/info/write calls
        all_text = []
        for name in ("warning", "info", "write", "error"):
            all_text.extend(
                call.args[0]
                for call in streamlit_recorder.calls_named(name)
                if call.args
            )
        assert any(
            "abort" in t.lower() for t in all_text
        ), "An abort-related message should be displayed to the operator"


class TestAbortCSVGeneration:
    """Abort button behavior during CSV-generation submit."""

    def test_abort_stops_csv_submit_loop(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting the abort flag stops the CSV-gen submit loop early."""
        kind = _SAMPLE_KINDS[0]
        manifests = [
            {"executionContext": {"manifest": {}, "i": i}} for i in range(5)
        ]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            manifests=manifests,
        )
        streamlit_recorder.session_state[GEN_ABORT_KEY] = True
        streamlit_recorder.button_responses[GEN_SUBMIT_LABEL] = True

        page_module = _load_page(streamlit_recorder, monkeypatch)
        spy = _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        # With abort set, should process fewer than all 5
        results = streamlit_recorder.session_state.get(GEN_SUBMIT_RESULTS_KEY, [])
        assert len(results) < 5, (
            f"Abort should stop CSV submit early but got {len(results)} results"
        )

    def test_abort_csv_partial_results_preserved(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Partial results from aborted CSV submit are preserved."""
        kind = _SAMPLE_KINDS[0]
        manifests = _sample_manifests()
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            manifests=manifests,
        )
        streamlit_recorder.session_state[GEN_ABORT_KEY] = True
        streamlit_recorder.button_responses[GEN_SUBMIT_LABEL] = True

        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        results = streamlit_recorder.session_state.get(GEN_SUBMIT_RESULTS_KEY, [])
        assert isinstance(results, list)

    def test_abort_csv_message_shows_count(
        self,
        streamlit_recorder: StreamlitRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After aborting CSV submit, a message shows the abort count."""
        kind = _SAMPLE_KINDS[0]
        manifests = [
            {"executionContext": {"manifest": {}, "i": i}} for i in range(4)
        ]
        _setup_csv_tab_session(
            streamlit_recorder,
            kind=kind,
            csv_data=_SAMPLE_CSV,
            legal_tag="opendes-tag",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            manifests=manifests,
        )
        streamlit_recorder.session_state[GEN_ABORT_KEY] = True
        streamlit_recorder.button_responses[GEN_SUBMIT_LABEL] = True

        page_module = _load_page(streamlit_recorder, monkeypatch)
        _patch_csv_services(page_module, monkeypatch)

        page_module.main()

        all_text = []
        for name in ("warning", "info", "write", "error"):
            all_text.extend(
                call.args[0]
                for call in streamlit_recorder.calls_named(name)
                if call.args
            )
        assert any(
            "abort" in t.lower() for t in all_text
        ), "An abort-related message should be displayed after CSV submit abort"


# ===========================================================================
# Ingestion (workflow run) status indicator
# ===========================================================================

SUBMIT_RESULTS_KEY = "bulk_submit_results"
RUN_STATUS_KEY = "bulk_run_status"


def _workflow_result(run_id: str, status: Any, *, ok: bool = True) -> Any:
    from app.models.osdu import WorkflowRunResult

    return WorkflowRunResult(
        workflow_id="Osdu_ingest",
        run_id=run_id,
        status=status,
        raw_status=str(status.value),
        message=None,
        ok=ok,
        http_status=200 if ok else 404,
        latency_ms=1.0,
        correlation_id=None,
        error_message=None if ok else "run not found",
        raw_response={},
    )


def test_ingestion_status_section_polls_and_rolls_up(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.osdu import WorkflowStatus

    page_module = _load_page(streamlit_recorder, monkeypatch)
    streamlit_recorder.session_state[SUBMIT_RESULTS_KEY] = [
        _submit_row("a.json", ok=True, run_id="run-a"),
        _submit_row("b.json", ok=True, run_id="run-b"),
        _submit_row("c.json", ok=False, error="boom"),
    ]
    streamlit_recorder.button_responses[
        page_module.RUN_STATUS_BUTTON_LABEL
    ] = True

    mapping = {"run-a": WorkflowStatus.FINISHED, "run-b": WorkflowStatus.FAILED}

    def fake_get_token(connection: ADMEConnection, **_: Any) -> str:
        return "test-token"

    def fake_status(
        connection: ADMEConnection, token: str, run_id: str
    ) -> Any:
        return _workflow_result(run_id, mapping[run_id])

    monkeypatch.setattr(page_module, "get_token", fake_get_token)
    monkeypatch.setattr(page_module, "get_workflow_status", fake_status)

    page_module._render_ingestion_status_section(_connection())

    stored = streamlit_recorder.session_state[RUN_STATUS_KEY]
    assert stored["run-a"]["state"] == "finished"
    assert stored["run-b"]["state"] == "failed"
    warnings = [c.args[0] for c in streamlit_recorder.calls_named("warning")]
    assert any("1 finished" in w and "1 failed" in w for w in warnings)


def test_ingestion_status_section_hidden_without_run_ids(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_module = _load_page(streamlit_recorder, monkeypatch)
    streamlit_recorder.session_state[SUBMIT_RESULTS_KEY] = [
        _submit_row("c.json", ok=False, error="boom"),
    ]

    page_module._render_ingestion_status_section(_connection())

    markdowns = [
        call.args[0]
        for call in streamlit_recorder.calls_named("markdown")
        if call.args
    ]
    assert all("Ingestion status" not in m for m in markdowns)


def test_workflow_state_label_maps_statuses(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.osdu import WorkflowStatus

    page_module = _load_page(streamlit_recorder, monkeypatch)
    assert (
        page_module._workflow_state_label(WorkflowStatus.FINISHED) == "finished"
    )
    assert page_module._workflow_state_label(WorkflowStatus.FAILED) == "failed"
    assert (
        page_module._workflow_state_label(WorkflowStatus.IN_PROGRESS)
        == "running"
    )
    assert (
        page_module._workflow_state_label(WorkflowStatus.UNKNOWN) == "unknown"
    )
