"""Tests for the work-product loader (upload blobs + submit manifests).

Pure transforms run against synthetic manifests; the submit orchestration
monkeypatches the File Service + workflow primitives as bound into the
``work_product_loader`` namespace, so no real HTTP is attempted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import (
    UploadBytesResult,
    UploadURLResult,
    WorkflowRunResult,
    WorkflowStatus,
)
from app.services import work_product_loader as wpl


def _connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        auth_method=AuthMethod.USER_IMPERSONATION,
        client_secret="",
    )


def _wp_manifest(
    file_source: str,
    *,
    wellbore_id: str = "osdu:master-data--Wellbore:1013:",
) -> dict[str, Any]:
    return {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {
            "WorkProduct": {
                "kind": "osdu:wks:work-product--WorkProduct:1.0.0",
                "acl": {"owners": ["placeholder@test"], "viewers": []},
                "legal": {"legaltags": ["placeholder-legal"]},
                "data": {"Components": ["surrogate-key:wpc-1"]},
            },
            "WorkProductComponents": [
                {
                    "id": "surrogate-key:wpc-1",
                    "kind": "osdu:wks:work-product-component--WellLog:1.0.0",
                    "acl": {"owners": [], "viewers": []},
                    "legal": {"legaltags": []},
                    "data": {
                        "Datasets": ["surrogate-key:file-1"],
                        "WellboreID": wellbore_id,
                    },
                }
            ],
            "Datasets": [
                {
                    "id": "surrogate-key:file-1",
                    "kind": "osdu:wks:dataset--File.Generic:1.0.0",
                    "acl": {"owners": [], "viewers": []},
                    "legal": {"legaltags": []},
                    "data": {
                        "DatasetProperties": {
                            "FileSourceInfo": {
                                "FileSource": file_source,
                                "PreloadFilePath": file_source,
                                "Name": Path(file_source).name,
                            }
                        }
                    },
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# resolve_local_file
# ---------------------------------------------------------------------------


def test_resolve_local_file_uses_parent_directory(tmp_path: Path) -> None:
    root = tmp_path / "datasets"
    (root / "markers").mkdir(parents=True)
    (root / "trajectories").mkdir(parents=True)
    (root / "markers" / "1000.csv").write_text("m", encoding="utf-8")
    (root / "trajectories" / "1000.csv").write_text("t", encoding="utf-8")

    marker = wpl.resolve_local_file(
        "s3://bucket/provided/markers/1000.csv", datasets_root=root
    )
    traj = wpl.resolve_local_file(
        "s3://bucket/provided/trajectories/1000.csv", datasets_root=root
    )
    assert marker == root / "markers" / "1000.csv"
    assert traj == root / "trajectories" / "1000.csv"


def test_resolve_local_file_applies_alias(tmp_path: Path) -> None:
    root = tmp_path / "datasets"
    (root / "documents").mkdir(parents=True)
    (root / "documents" / "FI.pdf").write_bytes(b"pdf")

    resolved = wpl.resolve_local_file(
        "s3://bucket/provided/USGS_docs/FI.pdf", datasets_root=root
    )
    assert resolved == root / "documents" / "FI.pdf"


def test_resolve_local_file_version_suffix_fallback(tmp_path: Path) -> None:
    root = tmp_path / "datasets"
    (root / "well-logs").mkdir(parents=True)
    (root / "well-logs" / "a.las").write_bytes(b"las")

    resolved = wpl.resolve_local_file(
        "s3://bucket/provided/well-logs_1_1_0/a.las", datasets_root=root
    )
    assert resolved == root / "well-logs" / "a.las"


def test_resolve_local_file_missing_returns_none(tmp_path: Path) -> None:
    assert (
        wpl.resolve_local_file(
            "s3://bucket/provided/markers/nope.csv",
            datasets_root=tmp_path / "datasets",
        )
        is None
    )


# ---------------------------------------------------------------------------
# collect / apply file sources + stamping
# ---------------------------------------------------------------------------


def test_collect_and_apply_file_sources() -> None:
    body = _wp_manifest("s3://bucket/provided/well-logs/a.las")
    assert wpl.collect_file_sources(body) == [
        "s3://bucket/provided/well-logs/a.las"
    ]

    wpl.apply_uploaded_file_sources(body, ["staged-token-1"])
    info = body["Data"]["Datasets"][0]["data"]["DatasetProperties"][
        "FileSourceInfo"
    ]
    assert info["FileSource"] == "staged-token-1"
    assert "PreloadFilePath" not in info


def test_stamp_work_product_acl_legal_overwrites_all_records() -> None:
    body = _wp_manifest("s3://bucket/provided/well-logs/a.las")
    wpl.stamp_work_product_acl_legal(
        body,
        acl_owners=["data.owners@x"],
        acl_viewers=["data.viewers@x"],
        legal_tag="real-legal",
        overwrite=True,
    )
    wp = body["Data"]["WorkProduct"]
    wpc = body["Data"]["WorkProductComponents"][0]
    ds = body["Data"]["Datasets"][0]
    assert wp["acl"]["owners"] == ["data.owners@x"]  # placeholder replaced
    assert wp["legal"]["legaltags"] == ["real-legal"]
    assert wpc["acl"]["viewers"] == ["data.viewers@x"]
    assert ds["acl"]["owners"] == ["data.owners@x"]


# ---------------------------------------------------------------------------
# submit_work_products
# ---------------------------------------------------------------------------


def _ok_url(file_source: str) -> UploadURLResult:
    return UploadURLResult(
        ok=True,
        http_status=200,
        signed_url="https://blob.example/sas?sig=x",
        file_source=file_source,
        file_id="fid",
    )


def _ok_bytes(size: int) -> UploadBytesResult:
    return UploadBytesResult(ok=True, http_status=201, bytes_uploaded=size)


def _ok_workflow(run_id: str = "wp-run-1") -> WorkflowRunResult:
    return WorkflowRunResult(
        workflow_id="Osdu_ingest",
        run_id=run_id,
        status=WorkflowStatus.IN_PROGRESS,
        raw_status="submitted",
        message=None,
        ok=True,
        http_status=200,
        latency_ms=5.0,
        correlation_id="c",
        error_message=None,
        raw_response={"runId": run_id},
    )


def _patch_ok(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    spy: dict[str, list[Any]] = {"url": [], "bytes": [], "submit": []}

    def fake_url(connection: ADMEConnection, token: str) -> UploadURLResult:
        spy["url"].append(token)
        return _ok_url(file_source=f"staged-{len(spy['url'])}")

    def fake_bytes(signed_url: str, file_bytes: bytes, **kw: Any) -> UploadBytesResult:
        spy["bytes"].append(len(file_bytes))
        return _ok_bytes(len(file_bytes))

    def fake_submit(
        connection: ADMEConnection, token: str, payload: dict[str, Any]
    ) -> WorkflowRunResult:
        spy["submit"].append(payload)
        return _ok_workflow(run_id=f"wp-run-{len(spy['submit'])}")

    monkeypatch.setattr(wpl, "get_upload_url", fake_url)
    monkeypatch.setattr(wpl, "upload_file_bytes", fake_bytes)
    monkeypatch.setattr(wpl, "submit_manifest", fake_submit)
    return spy


def _write_manifest(dir_: Path, name: str, body: dict[str, Any]) -> Path:
    path = dir_ / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_submit_work_products_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_ok(monkeypatch)
    datasets_root = tmp_path / "datasets"
    (datasets_root / "well-logs").mkdir(parents=True)
    (datasets_root / "well-logs" / "a.las").write_bytes(b"log-bytes")

    manifests_dir = tmp_path / "wp"
    manifests_dir.mkdir()
    body = _wp_manifest("s3://bucket/provided/well-logs/a.las")
    path = _write_manifest(manifests_dir, "load_log.json", body)

    results = list(
        wpl.submit_work_products(
            [path],
            datasets_root=datasets_root,
            acl_owners=["data.owners@x"],
            acl_viewers=["data.viewers@x"],
            legal_tag="real-legal",
            data_partition_id="example-opendes",
            connection=_connection(),
            token="tok",
        )
    )

    assert len(results) == 1
    assert results[0].status == "success"
    assert results[0].run_id == "wp-run-1"
    assert spy["bytes"] == [len(b"log-bytes")]

    # The submitted manifest carries the staged token + real ACL, not the
    # placeholder s3 path / testcompany groups.
    submitted = spy["submit"][0]["executionContext"]["manifest"]
    info = submitted["Data"]["Datasets"][0]["data"]["DatasetProperties"][
        "FileSourceInfo"
    ]
    assert info["FileSource"] == "staged-1"
    assert "PreloadFilePath" not in info
    assert submitted["Data"]["WorkProduct"]["acl"]["owners"] == [
        "data.owners@x"
    ]


def test_submit_work_products_missing_file_errors_without_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_ok(monkeypatch)
    datasets_root = tmp_path / "datasets"
    datasets_root.mkdir()
    manifests_dir = tmp_path / "wp"
    manifests_dir.mkdir()
    body = _wp_manifest("s3://bucket/provided/well-logs/missing.las")
    path = _write_manifest(manifests_dir, "load_log.json", body)

    results = list(
        wpl.submit_work_products(
            [path],
            datasets_root=datasets_root,
            acl_owners=["o"],
            acl_viewers=["v"],
            legal_tag="lt",
            data_partition_id="p",
            connection=_connection(),
            token="tok",
        )
    )

    assert results[0].status == "error"
    assert "not found" in (results[0].error or "").lower()
    assert spy["submit"] == []  # never submitted


def test_submit_work_products_upload_failure_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_ok(monkeypatch)
    monkeypatch.setattr(
        wpl,
        "upload_file_bytes",
        lambda signed_url, file_bytes, **kw: UploadBytesResult(
            ok=False, http_status=403, error_message="blob-403"
        ),
    )
    datasets_root = tmp_path / "datasets"
    (datasets_root / "well-logs").mkdir(parents=True)
    (datasets_root / "well-logs" / "a.las").write_bytes(b"x")
    manifests_dir = tmp_path / "wp"
    manifests_dir.mkdir()
    path = _write_manifest(
        manifests_dir,
        "load_log.json",
        _wp_manifest("s3://bucket/provided/well-logs/a.las"),
    )

    results = list(
        wpl.submit_work_products(
            [path],
            datasets_root=datasets_root,
            acl_owners=["o"],
            acl_viewers=["v"],
            legal_tag="lt",
            data_partition_id="p",
            connection=_connection(),
            token="tok",
        )
    )

    assert results[0].status == "error"
    assert results[0].error == "blob-403"
    assert spy["submit"] == []


def test_submit_work_products_load_prefix_rewrites_wellbore_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_ok(monkeypatch)
    datasets_root = tmp_path / "datasets"
    (datasets_root / "well-logs").mkdir(parents=True)
    (datasets_root / "well-logs" / "a.las").write_bytes(b"x")
    manifests_dir = tmp_path / "wp"
    manifests_dir.mkdir()
    path = _write_manifest(
        manifests_dir,
        "load_log.json",
        _wp_manifest("s3://bucket/provided/well-logs/a.las"),
    )

    # An independent (prefixed) load: the master-data tier was loaded under
    # this prefix, so the WP's Wellbore reference must follow.
    list(
        wpl.submit_work_products(
            [path],
            datasets_root=datasets_root,
            acl_owners=["o"],
            acl_viewers=["v"],
            legal_tag="lt",
            data_partition_id="p",
            connection=_connection(),
            token="tok",
            load_prefix="20260730-",
        )
    )

    submitted = spy["submit"][0]["executionContext"]["manifest"]
    wpc = submitted["Data"]["WorkProductComponents"][0]
    assert wpc["data"]["WellboreID"] == (
        "osdu:master-data--Wellbore:20260730-1013:"
    )


def test_submit_work_products_no_prefix_leaves_refs_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_ok(monkeypatch)
    datasets_root = tmp_path / "datasets"
    (datasets_root / "well-logs").mkdir(parents=True)
    (datasets_root / "well-logs" / "a.las").write_bytes(b"x")
    manifests_dir = tmp_path / "wp"
    manifests_dir.mkdir()
    path = _write_manifest(
        manifests_dir,
        "load_log.json",
        _wp_manifest("s3://bucket/provided/well-logs/a.las"),
    )

    list(
        wpl.submit_work_products(
            [path],
            datasets_root=datasets_root,
            acl_owners=["o"],
            acl_viewers=["v"],
            legal_tag="lt",
            data_partition_id="p",
            connection=_connection(),
            token="tok",
        )
    )

    submitted = spy["submit"][0]["executionContext"]["manifest"]
    wpc = submitted["Data"]["WorkProductComponents"][0]
    assert wpc["data"]["WellboreID"] == "osdu:master-data--Wellbore:1013:"


def test_submit_work_products_continues_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ok(monkeypatch)
    datasets_root = tmp_path / "datasets"
    (datasets_root / "well-logs").mkdir(parents=True)
    (datasets_root / "well-logs" / "a.las").write_bytes(b"a")
    # second manifest references a missing file
    manifests_dir = tmp_path / "wp"
    manifests_dir.mkdir()
    good = _write_manifest(
        manifests_dir,
        "good.json",
        _wp_manifest("s3://bucket/provided/well-logs/a.las"),
    )
    bad = _write_manifest(
        manifests_dir,
        "bad.json",
        _wp_manifest("s3://bucket/provided/well-logs/missing.las"),
    )

    results = list(
        wpl.submit_work_products(
            [good, bad],
            datasets_root=datasets_root,
            acl_owners=["o"],
            acl_viewers=["v"],
            legal_tag="lt",
            data_partition_id="p",
            connection=_connection(),
            token="tok",
        )
    )

    assert [r.status for r in results] == ["success", "error"]
