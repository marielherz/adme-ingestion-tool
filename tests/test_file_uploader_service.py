"""Tests for the bulk file-upload service (upload_files).

Fast + hermetic: the three File Service primitives
(``get_upload_url`` / ``upload_file_bytes`` / ``post_file_metadata``)
are monkeypatched as bound into the ``file_uploader`` module namespace,
so no real HTTP is attempted. Only the orchestration logic is exercised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import (
    FileMetadataResult,
    FileUploadOutcome,
    UploadBytesResult,
    UploadURLResult,
)
from app.services import file_uploader
from app.services.file_uploader import FileUploadItem, upload_files


def _connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        auth_method=AuthMethod.USER_IMPERSONATION,
        client_secret="",
    )


def _ok_url(file_source: str = "src-1", file_id: str = "fid-1") -> UploadURLResult:
    return UploadURLResult(
        ok=True,
        http_status=200,
        latency_ms=1.0,
        correlation_id="c1",
        signed_url="https://blob.example/sas?sig=x",
        file_source=file_source,
        file_id=file_id,
    )


def _ok_bytes(size: int) -> UploadBytesResult:
    return UploadBytesResult(
        ok=True, http_status=201, latency_ms=2.0, bytes_uploaded=size
    )


def _ok_meta(
    record_id: str = "opendes:dataset--File.Generic:abc",
) -> FileMetadataResult:
    return FileMetadataResult(
        ok=True,
        http_status=201,
        latency_ms=3.0,
        correlation_id="c2",
        record_id=record_id,
        record_version=1,
    )


def _write(tmp_path: Path, name: str, data: bytes = b"payload") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _patch_all_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[Any]]:
    """Patch the three primitives to succeed; return a call spy."""
    spy: dict[str, list[Any]] = {"url": [], "bytes": [], "meta": []}

    def fake_url(connection: ADMEConnection, token: str) -> UploadURLResult:
        spy["url"].append((connection, token))
        return _ok_url(file_source=f"src-{len(spy['url'])}")

    def fake_bytes(
        signed_url: str,
        file_bytes: bytes,
        *,
        content_type: str = "application/octet-stream",
        timeout: int = 120,
    ) -> UploadBytesResult:
        spy["bytes"].append(
            {
                "signed_url": signed_url,
                "size": len(file_bytes),
                "content_type": content_type,
                "timeout": timeout,
            }
        )
        return _ok_bytes(len(file_bytes))

    def fake_meta(
        connection: ADMEConnection,
        token: str,
        **kwargs: Any,
    ) -> FileMetadataResult:
        spy["meta"].append(kwargs)
        return _ok_meta(record_id=f"opendes:dataset--File.Generic:{len(spy['meta'])}")

    monkeypatch.setattr(file_uploader, "get_upload_url", fake_url)
    monkeypatch.setattr(file_uploader, "upload_file_bytes", fake_bytes)
    monkeypatch.setattr(file_uploader, "post_file_metadata", fake_meta)
    return spy


def test_guess_content_type_uses_suffix_then_default() -> None:
    assert file_uploader.guess_content_type(Path("a.txt")) == "text/plain"
    assert (
        file_uploader.guess_content_type(Path("a.unknownext"))
        == file_uploader.DEFAULT_CONTENT_TYPE
    )


def test_upload_files_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_all_ok(monkeypatch)
    path = _write(tmp_path, "log.las", b"0123456789")
    progress: list[FileUploadOutcome] = []

    outcomes = list(
        upload_files(
            _connection(),
            "tok",
            [FileUploadItem(path=path)],
            legal_tag="opendes-tno",
            acl_owners="data.x.owners@x",
            acl_viewers="data.x.viewers@x",
            progress_callback=progress.append,
        )
    )

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status == "success"
    assert outcome.record_id == "opendes:dataset--File.Generic:1"
    assert outcome.record_version == 1
    assert outcome.file_source == "src-1"
    assert outcome.bytes_uploaded == 10
    assert outcome.error is None
    # progress callback fired with the same outcome.
    assert progress == outcomes

    # display_name falls back to the filename; ACL/legal forwarded.
    meta_kwargs = spy["meta"][0]
    assert meta_kwargs["display_name"] == "log.las"
    assert meta_kwargs["file_source"] == "src-1"
    assert meta_kwargs["legal_tag"] == "opendes-tno"
    assert meta_kwargs["acl_owners"] == "data.x.owners@x"
    assert meta_kwargs["acl_viewers"] == "data.x.viewers@x"


def test_upload_files_passes_display_name_and_guessed_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_all_ok(monkeypatch)
    path = _write(tmp_path, "doc.txt")

    list(
        upload_files(
            _connection(),
            "tok",
            [FileUploadItem(path=path, display_name="My Doc", description="d")],
            legal_tag="lt",
            acl_owners="o",
            acl_viewers="v",
        )
    )

    assert spy["bytes"][0]["content_type"] == "text/plain"
    assert spy["bytes"][0]["timeout"] == file_uploader.UPLOAD_BYTES_TIMEOUT_SECONDS
    assert spy["meta"][0]["display_name"] == "My Doc"
    assert spy["meta"][0]["description"] == "d"


def test_upload_files_explicit_content_type_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_all_ok(monkeypatch)
    path = _write(tmp_path, "seismic.segy")

    list(
        upload_files(
            _connection(),
            "tok",
            [FileUploadItem(path=path, content_type="application/x-segy")],
            legal_tag="lt",
            acl_owners="o",
            acl_viewers="v",
        )
    )

    assert spy["bytes"][0]["content_type"] == "application/x-segy"


def test_upload_files_missing_file_yields_error_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_all_ok(monkeypatch)
    missing = tmp_path / "nope.las"

    outcomes = list(
        upload_files(
            _connection(),
            "tok",
            [FileUploadItem(path=missing)],
            legal_tag="lt",
            acl_owners="o",
            acl_viewers="v",
        )
    )

    assert outcomes[0].status == "error"
    assert "Cannot read file" in (outcomes[0].error or "")
    assert spy["url"] == []  # never reached the network


def test_upload_files_empty_file_yields_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_all_ok(monkeypatch)
    path = _write(tmp_path, "empty.las", b"")

    outcomes = list(
        upload_files(
            _connection(),
            "tok",
            [FileUploadItem(path=path)],
            legal_tag="lt",
            acl_owners="o",
            acl_viewers="v",
        )
    )

    assert outcomes[0].status == "error"
    assert "empty" in (outcomes[0].error or "").lower()
    assert spy["url"] == []


def test_upload_files_upload_url_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(
        file_uploader,
        "get_upload_url",
        lambda connection, token: UploadURLResult(
            ok=False, http_status=500, error_message="boom-url"
        ),
    )
    path = _write(tmp_path, "log.las")

    outcomes = list(
        upload_files(
            _connection(),
            "tok",
            [FileUploadItem(path=path)],
            legal_tag="lt",
            acl_owners="o",
            acl_viewers="v",
        )
    )

    assert outcomes[0].status == "error"
    assert outcomes[0].error == "boom-url"
    assert outcomes[0].record_id is None


def test_upload_files_bytes_failure_keeps_file_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(
        file_uploader,
        "upload_file_bytes",
        lambda signed_url, file_bytes, **kw: UploadBytesResult(
            ok=False, http_status=403, error_message="blob-403"
        ),
    )
    path = _write(tmp_path, "log.las")

    outcomes = list(
        upload_files(
            _connection(),
            "tok",
            [FileUploadItem(path=path)],
            legal_tag="lt",
            acl_owners="o",
            acl_viewers="v",
        )
    )

    assert outcomes[0].status == "error"
    assert outcomes[0].error == "blob-403"
    assert outcomes[0].file_source == "src-1"
    assert outcomes[0].record_id is None


def test_upload_files_metadata_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(
        file_uploader,
        "post_file_metadata",
        lambda connection, token, **kw: FileMetadataResult(
            ok=False, http_status=400, error_message="meta-400"
        ),
    )
    path = _write(tmp_path, "log.las", b"12345")

    outcomes = list(
        upload_files(
            _connection(),
            "tok",
            [FileUploadItem(path=path)],
            legal_tag="lt",
            acl_owners="o",
            acl_viewers="v",
        )
    )

    assert outcomes[0].status == "error"
    assert outcomes[0].error == "meta-400"
    assert outcomes[0].bytes_uploaded == 5  # bytes did upload
    assert outcomes[0].file_source == "src-1"


def test_upload_files_continues_after_one_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_ok(monkeypatch)

    # Fail the second file's byte upload only.
    calls = {"n": 0}
    real_ok = UploadBytesResult(
        ok=True, http_status=201, latency_ms=1.0, bytes_uploaded=7
    )

    def flaky_bytes(signed_url, file_bytes, **kw) -> UploadBytesResult:
        calls["n"] += 1
        if calls["n"] == 2:
            return UploadBytesResult(
                ok=False, http_status=500, error_message="second-fails"
            )
        return real_ok

    monkeypatch.setattr(file_uploader, "upload_file_bytes", flaky_bytes)

    items = [
        FileUploadItem(path=_write(tmp_path, "a.las", b"aaa")),
        FileUploadItem(path=_write(tmp_path, "b.las", b"bbb")),
        FileUploadItem(path=_write(tmp_path, "c.las", b"ccc")),
    ]

    outcomes = list(
        upload_files(
            _connection(),
            "tok",
            items,
            legal_tag="lt",
            acl_owners="o",
            acl_viewers="v",
        )
    )

    statuses = [o.status for o in outcomes]
    assert statuses == ["success", "error", "success"]
    assert outcomes[1].error == "second-fails"
