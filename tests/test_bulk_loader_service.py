"""Tests for the Bulk Load service (registry, preview, submit_tier).

Style mirror of ``test_ingestion_service.py``: stdlib-only assertions
where possible, ``monkeypatch`` for HTTP boundaries, and a hand-rolled
``_connection`` helper to avoid pulling Streamlit context into the tests.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import (
    ManifestPreview,
    SubmitResult,
    WorkflowRunResult,
    WorkflowStatus,
)
from app.services import bulk_loader


def _connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        auth_method=AuthMethod.USER_IMPERSONATION,
        client_secret="",
    )


def _ok_result(run_id: str = "run-1") -> WorkflowRunResult:
    return WorkflowRunResult(
        workflow_id="Osdu_ingest",
        run_id=run_id,
        status=WorkflowStatus.IN_PROGRESS,
        raw_status="submitted",
        message=None,
        ok=True,
        http_status=200,
        latency_ms=12.3,
        correlation_id="corr-1",
        error_message=None,
        raw_response={"runId": run_id, "recordId": "rec-1"},
    )


def _err_result(message: str = "boom") -> WorkflowRunResult:
    return WorkflowRunResult(
        workflow_id=None,
        run_id=None,
        status=WorkflowStatus.FAILED,
        raw_status="failed",
        message=None,
        ok=False,
        http_status=500,
        latency_ms=4.0,
        correlation_id=None,
        error_message=message,
        raw_response=None,
    )


@pytest.fixture(autouse=True)
def _clear_dataset_cache() -> Iterator[None]:
    bulk_loader._clear_cache()
    yield
    bulk_loader._clear_cache()


# ---------------------------------------------------------------------------
# list_datasets / load_dataset
# ---------------------------------------------------------------------------


def test_list_datasets_discovers_tno_and_volve_sorted() -> None:
    datasets = bulk_loader.list_datasets()
    ids = [d.id for d in datasets]

    assert "tno" in ids
    assert "volve" in ids
    # display_name sort, case-insensitive
    display_names = [d.display_name for d in datasets]
    assert display_names == sorted(display_names, key=str.lower)
    by_id = {d.id: d for d in datasets}
    assert by_id["tno"].display_name == "TNO Open Test Data"
    assert by_id["volve"].display_name == "Volve Open Dataset"


def test_list_datasets_is_cached() -> None:
    first = bulk_loader.list_datasets()
    second = bulk_loader.list_datasets()
    assert [d.id for d in first] == [d.id for d in second]


def test_list_datasets_skips_malformed_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Build an isolated fake datasets root with one good + one bad entry.
    fake_data_root = tmp_path / "data"
    fake_datasets = fake_data_root / "datasets"
    good = fake_datasets / "good"
    bad = fake_datasets / "bad"
    good.mkdir(parents=True)
    bad.mkdir(parents=True)
    (good / "dataset.json").write_text(
        json.dumps(
            {
                "id": "good",
                "display_name": "Good Dataset",
                "source_url": "https://example.invalid",
                "notice_path": "NOTICE.md",
                "tiers": {"reference-data": {"enabled": False, "reason": "x"}},
            }
        ),
        encoding="utf-8",
    )
    (bad / "dataset.json").write_text("{ not valid json", encoding="utf-8")

    monkeypatch.setattr(bulk_loader, "DATA_ROOT", fake_data_root.resolve())
    monkeypatch.setattr(bulk_loader, "DATASETS_ROOT", fake_datasets.resolve())
    bulk_loader._clear_cache()

    with caplog.at_level("WARNING"):
        datasets = bulk_loader.list_datasets()

    assert [d.id for d in datasets] == ["good"]
    assert any(
        "malformed dataset descriptor" in record.message.lower()
        for record in caplog.records
    )


def test_load_dataset_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown dataset id"):
        bulk_loader.load_dataset("nonexistent-dataset")


# ---------------------------------------------------------------------------
# preview_tier
# ---------------------------------------------------------------------------


def test_preview_tier_tno_reference_data() -> None:
    previews = bulk_loader.preview_tier("tno", "reference-data")
    assert len(previews) == 13
    for preview in previews:
        assert isinstance(preview, ManifestPreview)
        assert preview.filename.startswith("load_")
        assert preview.filename.endswith(".json")
        assert preview.kind, f"missing kind on {preview.filename}"
        assert preview.record_count > 0
        assert preview.record_section == "ReferenceData"


def test_preview_tier_disabled_raises() -> None:
    with pytest.raises(ValueError, match="disabled"):
        bulk_loader.preview_tier("tno", "work-products")


def test_preview_tier_unknown_tier_raises() -> None:
    with pytest.raises(ValueError, match="no tier"):
        bulk_loader.preview_tier("tno", "nope")


def test_preview_tier_path_traversal_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_data_root = tmp_path / "data"
    fake_datasets = fake_data_root / "datasets"
    evil = fake_datasets / "evil"
    evil.mkdir(parents=True)
    (evil / "dataset.json").write_text(
        json.dumps(
            {
                "id": "evil",
                "display_name": "Evil",
                "source_url": "https://example.invalid",
                "notice_path": "NOTICE.md",
                "tiers": {
                    "reference-data": {
                        "enabled": True,
                        "manifest_glob": "../../../etc/passwd",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(bulk_loader, "DATA_ROOT", fake_data_root.resolve())
    monkeypatch.setattr(bulk_loader, "DATASETS_ROOT", fake_datasets.resolve())
    bulk_loader._clear_cache()

    with pytest.raises(ValueError, match="escapes the app/data/ sandbox"):
        bulk_loader.preview_tier("evil", "reference-data")


def test_preview_tier_unreadable_manifest_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_paths = _build_synthetic_tno(tmp_path, monkeypatch, file_count=1)
    manifest_paths[0].write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="Cannot preview manifest load_0.json"):
        bulk_loader.preview_tier("tno", "reference-data")


# ---------------------------------------------------------------------------
# submit_tier
# ---------------------------------------------------------------------------


def _build_synthetic_tno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_count: int = 2
) -> list[Path]:
    """Set DATA_ROOT/DATASETS_ROOT to a tmp tree with N tiny manifests."""
    fake_data_root = tmp_path / "data"
    fake_datasets = fake_data_root / "datasets"
    dataset_dir = fake_datasets / "tno"
    manifest_dir = fake_data_root / "manifests"
    dataset_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)

    (dataset_dir / "dataset.json").write_text(
        json.dumps(
            {
                "id": "tno",
                "display_name": "TNO Open Test Data",
                "source_url": "https://example.invalid",
                "notice_path": "NOTICE.md",
                "tiers": {
                    "reference-data": {
                        "enabled": True,
                        "manifest_glob": "../../manifests/load_*.json",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    manifest_paths: list[Path] = []
    for i in range(file_count):
        body = {
            "kind": "osdu:wks:Manifest:1.0.0",
            "ReferenceData": [
                {
                    "id": f"osdu:reference-data--Foo:item-{i}",
                    "kind": "osdu:wks:reference-data--Foo:1.0.0",
                    "data": {"Name": f"item-{i}"},
                    "acl": {"owners": [], "viewers": []},
                    "legal": {"legaltags": [], "otherRelevantDataCountries": []},
                }
            ],
        }
        path = manifest_dir / f"load_{i}.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        manifest_paths.append(path)

    monkeypatch.setattr(bulk_loader, "DATA_ROOT", fake_data_root.resolve())
    monkeypatch.setattr(bulk_loader, "DATASETS_ROOT", fake_datasets.resolve())
    bulk_loader._clear_cache()
    return manifest_paths


def test_submit_tier_calls_submit_manifest_once_per_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_paths = _build_synthetic_tno(tmp_path, monkeypatch, file_count=3)

    calls: list[dict[str, Any]] = []

    def fake_submit(
        connection: ADMEConnection,
        token: str,
        manifest_payload: dict[str, Any],
    ) -> WorkflowRunResult:
        calls.append(manifest_payload)
        return _ok_result(run_id=f"run-{len(calls)}")

    monkeypatch.setattr(bulk_loader, "submit_manifest", fake_submit)

    results = list(
        bulk_loader.submit_tier(
            "tno",
            "reference-data",
            acl_owners=["data.default.owners@example"],
            acl_viewers=["data.default.viewers@example"],
            legal_tag="example-legal",
            data_partition_id="example-opendes",
            connection=_connection(),
            token="fake-token",
        )
    )

    assert len(results) == 3
    assert len(calls) == 3
    for result in results:
        assert isinstance(result, SubmitResult)
        assert result.status == "success"
        assert result.run_id is not None
        assert result.record_id == "rec-1"
        assert result.error is None
        assert result.manifest_path in manifest_paths


def test_submit_tier_records_run_history_for_accepted_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build_synthetic_tno(tmp_path, monkeypatch, file_count=1)

    submit_calls: list[dict[str, Any]] = []
    finish_calls: list[dict[str, Any]] = []

    def fake_submit(
        connection: ADMEConnection,
        token: str,
        manifest_payload: dict[str, Any],
    ) -> WorkflowRunResult:
        return _ok_result(run_id="bulk-run-1")

    def fake_record_submit(**kwargs: Any) -> None:
        submit_calls.append(kwargs)

    def fake_record_finish(**kwargs: Any) -> None:
        finish_calls.append(kwargs)

    monkeypatch.setattr(bulk_loader, "submit_manifest", fake_submit)
    monkeypatch.setattr(bulk_loader, "record_workflow_submit", fake_record_submit)
    monkeypatch.setattr(bulk_loader, "record_workflow_finish", fake_record_finish)

    results = list(
        bulk_loader.submit_tier(
            "tno",
            "reference-data",
            acl_owners=["owner@example"],
            acl_viewers=["viewer@example"],
            legal_tag="example-legal",
            data_partition_id="example-opendes",
            connection=_connection(),
            token="fake-token",
        )
    )

    assert [r.run_id for r in results] == ["bulk-run-1"]
    assert submit_calls == [
        {
            "run_id": "bulk-run-1",
            "submitted_at": submit_calls[0]["submitted_at"],
            "kind": "osdu:wks:Manifest:1.0.0",
            "correlation_id": "corr-1",
            "submit_source": "bulk_load",
            "data_partition_id": "example-opendes",
        }
    ]
    assert submit_calls[0]["submitted_at"].endswith("Z")
    assert finish_calls == []


def test_submit_tier_injects_acl_and_legal_into_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build_synthetic_tno(tmp_path, monkeypatch, file_count=1)

    captured: list[dict[str, Any]] = []

    def fake_submit(
        connection: ADMEConnection,
        token: str,
        manifest_payload: dict[str, Any],
    ) -> WorkflowRunResult:
        captured.append(manifest_payload)
        return _ok_result()

    monkeypatch.setattr(bulk_loader, "submit_manifest", fake_submit)

    list(
        bulk_loader.submit_tier(
            "tno",
            "reference-data",
            acl_owners=["owner-a@example", "owner-b@example"],
            acl_viewers=["viewer-a@example"],
            legal_tag="example-legal-tag",
            data_partition_id="example-opendes",
            connection=_connection(),
            token="fake-token",
        )
    )

    assert len(captured) == 1
    payload = captured[0]
    assert (
        payload["executionContext"]["Payload"]["data-partition-id"]
        == "example-opendes"
    )
    manifest = payload["executionContext"]["manifest"]
    record = manifest["ReferenceData"][0]
    assert record["acl"]["owners"] == ["owner-a@example", "owner-b@example"]
    assert record["acl"]["viewers"] == ["viewer-a@example"]
    assert record["legal"]["legaltags"] == ["example-legal-tag"]
    # otherRelevantDataCountries is untouched (not populated by injector)
    assert record["legal"]["otherRelevantDataCountries"] == []


def test_submit_tier_does_not_overwrite_populated_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = _build_synthetic_tno(tmp_path, monkeypatch, file_count=1)
    # Rewrite the single manifest to have pre-populated ACL/legal.
    body = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "ReferenceData": [
            {
                "id": "osdu:reference-data--Foo:already",
                "kind": "osdu:wks:reference-data--Foo:1.0.0",
                "data": {"Name": "x"},
                "acl": {
                    "owners": ["preset-owner@example"],
                    "viewers": ["preset-viewer@example"],
                },
                "legal": {
                    "legaltags": ["preset-tag"],
                    "otherRelevantDataCountries": [],
                },
            }
        ],
    }
    manifests[0].write_text(json.dumps(body), encoding="utf-8")

    captured: list[dict[str, Any]] = []

    def fake_submit(
        connection: ADMEConnection,
        token: str,
        manifest_payload: dict[str, Any],
    ) -> WorkflowRunResult:
        captured.append(manifest_payload)
        return _ok_result()

    monkeypatch.setattr(bulk_loader, "submit_manifest", fake_submit)

    list(
        bulk_loader.submit_tier(
            "tno",
            "reference-data",
            acl_owners=["other-owner@example"],
            acl_viewers=["other-viewer@example"],
            legal_tag="other-tag",
            data_partition_id="example-opendes",
            connection=_connection(),
            token="t",
        )
    )

    record = captured[0]["executionContext"]["manifest"]["ReferenceData"][0]
    assert record["acl"]["owners"] == ["preset-owner@example"]
    assert record["acl"]["viewers"] == ["preset-viewer@example"]
    assert record["legal"]["legaltags"] == ["preset-tag"]


def test_submit_tier_continues_past_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build_synthetic_tno(tmp_path, monkeypatch, file_count=3)

    call_log: list[str] = []

    def fake_submit(
        connection: ADMEConnection,
        token: str,
        manifest_payload: dict[str, Any],
    ) -> WorkflowRunResult:
        idx = len(call_log)
        call_log.append("call")
        # second call (index 1) fails; others succeed
        if idx == 1:
            return _err_result("workflow rejected")
        return _ok_result(run_id=f"run-{idx}")

    monkeypatch.setattr(bulk_loader, "submit_manifest", fake_submit)

    results = list(
        bulk_loader.submit_tier(
            "tno",
            "reference-data",
            acl_owners=["o@x"],
            acl_viewers=["v@x"],
            legal_tag="t",
            data_partition_id="p",
            connection=_connection(),
            token="tok",
        )
    )

    assert len(call_log) == 3
    assert len(results) == 3
    statuses = [r.status for r in results]
    errors = [r.error for r in results]
    assert statuses == ["success", "error", "success"]
    assert errors[0] is None
    assert errors[1] == "workflow rejected"
    assert errors[2] is None


# ---------------------------------------------------------------------------
# per-load prefix (Smart Tier independent copies)
# ---------------------------------------------------------------------------


def test_make_load_prefix_uses_date() -> None:
    from datetime import date

    assert bulk_loader.make_load_prefix(date(2026, 6, 30)) == "20260630-"


def _master_data_bodies() -> list[dict[str, Any]]:
    well = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "MasterData": [
            {
                "id": "osdu:master-data--Well:W1",
                "kind": "osdu:wks:master-data--Well:1.0.0",
                "data": {"FacilityName": "W1"},
            }
        ],
    }
    wellbore = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "MasterData": [
            {
                "id": "osdu:master-data--Wellbore:W1-S1",
                "kind": "osdu:wks:master-data--Wellbore:1.0.0",
                "data": {
                    "WellID": "<namespace>:master-data--Well:W1:",
                    "ExistenceKind": (
                        "<namespace>:reference-data--ExistenceKind:Active:"
                    ),
                },
            }
        ],
    }
    return [well, wellbore]


def test_apply_load_prefix_rewrites_ids_and_intra_load_references() -> None:
    bodies = _master_data_bodies()
    out = bulk_loader.apply_load_prefix(
        bodies, section="MasterData", prefix="20260630-"
    )

    well = out[0]["MasterData"][0]
    wellbore = out[1]["MasterData"][0]

    # Record ids carry the prefix on the unique-id portion only.
    assert well["id"] == "osdu:master-data--Well:20260630-W1"
    assert wellbore["id"] == "osdu:master-data--Wellbore:20260630-W1-S1"

    # The cross-manifest reference is rewritten consistently, preserving the
    # ``<namespace>`` token and the trailing version colon.
    assert (
        wellbore["data"]["WellID"]
        == "<namespace>:master-data--Well:20260630-W1:"
    )

    # A reference to a record outside this load (shared reference-data) is
    # left untouched so the link still resolves to the shared copy.
    assert (
        wellbore["data"]["ExistenceKind"]
        == "<namespace>:reference-data--ExistenceKind:Active:"
    )


def test_apply_load_prefix_does_not_touch_kind_strings() -> None:
    bodies = _master_data_bodies()
    out = bulk_loader.apply_load_prefix(
        bodies, section="MasterData", prefix="20260630-"
    )
    assert out[0]["MasterData"][0]["kind"] == "osdu:wks:master-data--Well:1.0.0"
    assert out[0]["kind"] == "osdu:wks:Manifest:1.0.0"


def test_apply_load_prefix_blank_prefix_is_noop_deep_copy() -> None:
    bodies = _master_data_bodies()
    out = bulk_loader.apply_load_prefix(bodies, section="MasterData", prefix="  ")

    assert out == bodies
    # A deep copy is returned so callers can mutate without aliasing.
    assert out[0] is not bodies[0]
    assert out[0]["MasterData"][0] is not bodies[0]["MasterData"][0]


def test_build_prefix_id_map_keys_on_type_and_unique() -> None:
    bodies = _master_data_bodies()
    id_map = bulk_loader.build_prefix_id_map(
        bodies, section="MasterData", prefix="20260630-"
    )
    assert id_map == {
        ("master-data--Well", "W1"): "20260630-W1",
        ("master-data--Wellbore", "W1-S1"): "20260630-W1-S1",
    }


def test_build_prefix_id_map_blank_prefix_is_empty() -> None:
    bodies = _master_data_bodies()
    assert (
        bulk_loader.build_prefix_id_map(
            bodies, section="MasterData", prefix=""
        )
        == {}
    )


def test_build_reference_prefix_map_targets_entity_prefix() -> None:
    # A work-product-shaped body referencing master-data + reference-data.
    body = {
        "Data": {
            "WorkProductComponents": [
                {
                    "id": "surrogate-key:wpc-1",
                    "data": {
                        "WellboreID": "osdu:master-data--Wellbore:1013:",
                        "OperatorID": "<namespace>:master-data--Organisation:EBN:",
                        "ExistenceKind": (
                            "<namespace>:reference-data--ExistenceKind:Active:"
                        ),
                    },
                }
            ]
        }
    }
    ref_map = bulk_loader.build_reference_prefix_map(
        body, prefix="20260730-", entity_prefix="master-data--"
    )
    assert ref_map == {
        ("master-data--Wellbore", "1013"): "20260730-1013",
        ("master-data--Organisation", "EBN"): "20260730-EBN",
    }
    # reference-data refs and surrogate-keys are excluded.
    assert ("reference-data--ExistenceKind", "Active") not in ref_map


def test_build_reference_prefix_map_blank_prefix_is_empty() -> None:
    body = {"data": {"WellboreID": "osdu:master-data--Wellbore:1013:"}}
    assert (
        bulk_loader.build_reference_prefix_map(
            body, prefix="  ", entity_prefix="master-data--"
        )
        == {}
    )


def test_submit_tier_with_load_prefix_rewrites_record_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build_synthetic_tno(tmp_path, monkeypatch, file_count=2)

    payloads: list[dict[str, Any]] = []

    def fake_submit(
        connection: ADMEConnection,
        token: str,
        manifest_payload: dict[str, Any],
    ) -> WorkflowRunResult:
        payloads.append(manifest_payload)
        return _ok_result(run_id=f"run-{len(payloads)}")

    monkeypatch.setattr(bulk_loader, "submit_manifest", fake_submit)

    list(
        bulk_loader.submit_tier(
            "tno",
            "reference-data",
            acl_owners=["o@x"],
            acl_viewers=["v@x"],
            legal_tag="t",
            data_partition_id="p",
            connection=_connection(),
            token="tok",
            load_prefix="20260630-",
        )
    )

    ids = [
        payload["executionContext"]["manifest"]["ReferenceData"][0]["id"]
        for payload in payloads
    ]
    assert ids == [
        "osdu:reference-data--Foo:20260630-item-0",
        "osdu:reference-data--Foo:20260630-item-1",
    ]


def test_submit_manifest_paths_uses_token_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build_synthetic_tno(tmp_path, monkeypatch, file_count=2)
    paths = sorted((tmp_path / "data" / "manifests").glob("load_*.json"))

    seen_tokens: list[str] = []

    def fake_submit(
        connection: ADMEConnection,
        token: str,
        manifest_payload: dict[str, Any],
    ) -> WorkflowRunResult:
        seen_tokens.append(token)
        return _ok_result(run_id=f"run-{len(seen_tokens)}")

    monkeypatch.setattr(bulk_loader, "submit_manifest", fake_submit)

    tokens = iter(["fresh-1", "fresh-2"])

    list(
        bulk_loader.submit_manifest_paths(
            paths,
            section="ReferenceData",
            acl_owners=["o@x"],
            acl_viewers=["v@x"],
            legal_tag="t",
            data_partition_id="p",
            connection=_connection(),
            token="stale-fixed",
            token_provider=lambda: next(tokens),
        )
    )

    # The provider's token was used per submit, not the fixed one.
    assert seen_tokens == ["fresh-1", "fresh-2"]


def test_submit_manifest_paths_token_provider_failure_is_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build_synthetic_tno(tmp_path, monkeypatch, file_count=1)
    paths = sorted((tmp_path / "data" / "manifests").glob("load_*.json"))

    monkeypatch.setattr(
        bulk_loader,
        "submit_manifest",
        lambda *a, **k: _ok_result(),
    )

    def boom() -> str:
        raise RuntimeError("az login expired")

    results = list(
        bulk_loader.submit_manifest_paths(
            paths,
            section="ReferenceData",
            acl_owners=["o@x"],
            acl_viewers=["v@x"],
            legal_tag="t",
            data_partition_id="p",
            connection=_connection(),
            token="tok",
            token_provider=boom,
        )
    )

    assert results[0].status == "error"
    assert "token refresh failed" in (results[0].error or "")


def test_submit_tier_without_prefix_leaves_ids_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build_synthetic_tno(tmp_path, monkeypatch, file_count=1)

    payloads: list[dict[str, Any]] = []

    def fake_submit(
        connection: ADMEConnection,
        token: str,
        manifest_payload: dict[str, Any],
    ) -> WorkflowRunResult:
        payloads.append(manifest_payload)
        return _ok_result()

    monkeypatch.setattr(bulk_loader, "submit_manifest", fake_submit)

    list(
        bulk_loader.submit_tier(
            "tno",
            "reference-data",
            acl_owners=["o@x"],
            acl_viewers=["v@x"],
            legal_tag="t",
            data_partition_id="p",
            connection=_connection(),
            token="tok",
        )
    )

    assert (
        payloads[0]["executionContext"]["manifest"]["ReferenceData"][0]["id"]
        == "osdu:reference-data--Foo:item-0"
    )
