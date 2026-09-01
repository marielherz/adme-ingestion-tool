"""Tests for ``app.services.files``: 3 HTTP functions + constants regression."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import requests  # type: ignore[import-untyped]

from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import (
    FileMetadataResult,
    UploadBytesResult,
    UploadURLResult,
)
from app.services import files as files_module
from app.services.files import (
    FILE_GENERIC_KIND,
    FILES_METADATA_PATH,
    FILES_TIMEOUT_SECONDS,
    FILES_UPLOAD_URL_PATH,
    MAX_FILE_BYTES_V1,
    check_file_blob,
    get_download_url,
    get_upload_url,
    post_file_metadata,
    upload_file_blocks,
    upload_file_bytes,
)

# ---------------------------------------------------------------------------
# Helpers (mirror the legal_tags test fixture pattern)
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

    monkeypatch.setattr(files_module.requests, method, fake)
    return captured


# ===========================================================================
# Constants regression
# ===========================================================================


def test_files_constants_are_stable() -> None:
    assert FILES_UPLOAD_URL_PATH == "/api/file/v2/files/uploadURL"
    assert FILES_METADATA_PATH == "/api/file/v2/files/metadata"
    assert MAX_FILE_BYTES_V1 == 100 * 1024 * 1024
    assert FILE_GENERIC_KIND == "osdu:wks:dataset--File.Generic:1.0.0"


# ===========================================================================
# get_upload_url
# ===========================================================================


def test_get_upload_url_happy_path_flat_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "get",
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-up-1"},
            json_payload={
                "SignedURL": "https://blob.example/sas?sig=xyz",
                "FileSource": "/abc/def",
                "FileID": "fid-1",
            },
        ),
    )

    result = get_upload_url(_connection(), token="t")

    assert isinstance(result, UploadURLResult)
    assert result.ok is True
    assert result.http_status == 200
    assert result.signed_url == "https://blob.example/sas?sig=xyz"
    assert result.file_source == "/abc/def"
    assert result.file_id == "fid-1"
    assert result.correlation_id == "corr-up-1"
    assert result.latency_ms >= 0.0
    assert captured[0]["url"].endswith(FILES_UPLOAD_URL_PATH)


def test_get_upload_url_happy_path_location_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy OSDU prototype wraps fields under a Location envelope."""
    _patch_method(
        monkeypatch,
        "get",
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "Location": {
                    "SignedURL": "https://blob/x",
                    "FileSource": "/src",
                }
            },
        ),
    )

    result = get_upload_url(_connection(), token="t")

    assert result.ok is True
    assert result.signed_url == "https://blob/x"
    assert result.file_source == "/src"


def test_get_upload_url_missing_signed_url_returns_helpful_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_method(
        monkeypatch,
        "get",
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={"FileSource": "/src"},  # No SignedURL
        ),
    )

    result = get_upload_url(_connection(), token="t")

    assert result.ok is False
    assert result.http_status == 200
    assert result.error_message is not None
    assert "SignedURL" in result.error_message or "FileSource" in result.error_message


def test_get_upload_url_missing_file_source_returns_helpful_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_method(
        monkeypatch,
        "get",
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={"SignedURL": "https://blob/x"},  # No FileSource
        ),
    )

    result = get_upload_url(_connection(), token="t")

    assert result.ok is False
    assert result.error_message is not None
    assert "FileSource" in result.error_message


@pytest.mark.parametrize("status_code", [401, 403, 404, 500])
def test_get_upload_url_http_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _patch_method(
        monkeypatch,
        "get",
        lambda **_: _FakeResponse(
            status_code=status_code,
            headers={"correlation-id": f"corr-{status_code}"},
            json_payload={"message": f"boom {status_code}"},
        ),
    )

    result = get_upload_url(_connection(), token="t")

    assert result.ok is False
    assert result.http_status == status_code
    assert result.correlation_id == f"corr-{status_code}"
    assert result.error_message is not None
    assert f"boom {status_code}" in result.error_message
    assert result.signed_url is None
    assert result.file_source is None


def test_get_upload_url_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(**_: Any) -> Any:
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(files_module.requests, "get", fake_get)
    result = get_upload_url(_connection(), token="t")

    assert result.ok is False
    assert result.http_status is None
    assert result.error_message is not None
    assert "timed out" in result.error_message.lower()
    assert str(FILES_TIMEOUT_SECONDS) in result.error_message


def test_get_upload_url_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(**_: Any) -> Any:
        raise requests.exceptions.ConnectionError("dns")

    monkeypatch.setattr(files_module.requests, "get", fake_get)
    result = get_upload_url(_connection(), token="t")

    assert result.ok is False
    assert result.http_status is None
    assert "ConnectionError" in (result.error_message or "")


def test_get_upload_url_outgoing_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "get",
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={"SignedURL": "u", "FileSource": "s"},
        ),
    )
    get_upload_url(_connection(), token="bearer-abc")
    headers = captured[0]["headers"]
    assert headers["Authorization"] == "Bearer bearer-abc"
    assert headers["data-partition-id"] == "example-opendes"
    assert headers["Accept"] == "application/json"
    assert "Content-Type" not in headers
    assert captured[0]["timeout"] == FILES_TIMEOUT_SECONDS
    assert captured[0]["allow_redirects"] is False


@pytest.mark.parametrize(
    "header_name",
    ["correlation-id", "X-Correlation-ID", "Request-Id", "X-Request-Id"],
)
def test_get_upload_url_correlation_id_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, header_name: str
) -> None:
    _patch_method(
        monkeypatch,
        "get",
        lambda **_: _FakeResponse(
            status_code=200,
            headers={header_name: "corr-x"},
            json_payload={"SignedURL": "u", "FileSource": "s"},
        ),
    )
    result = get_upload_url(_connection(), token="t")
    assert result.correlation_id == "corr-x"


@pytest.mark.parametrize("token", ["", "   ", "\t\n"])
def test_get_upload_url_rejects_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="non-empty bearer token"):
        get_upload_url(_connection(), token=token)


# ===========================================================================
# upload_file_bytes
# ===========================================================================


def test_upload_file_bytes_happy_path_201(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "put",
        lambda **_: _FakeResponse(status_code=201),
    )

    payload = b"hello world bytes"
    result = upload_file_bytes(
        "https://blob.example/sas?sig=xyz",
        payload,
        content_type="text/plain",
    )

    assert isinstance(result, UploadBytesResult)
    assert result.ok is True
    assert result.http_status == 201
    assert result.bytes_uploaded == len(payload)
    assert result.error_message is None
    assert result.latency_ms >= 0.0
    # Confirm body bytes were forwarded.
    assert captured[0]["data"] == payload


def test_upload_file_bytes_sends_blocktype_and_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "put",
        lambda **_: _FakeResponse(status_code=201),
    )
    upload_file_bytes(
        "https://blob.example/sas?sig=xyz",
        b"abc",
        content_type="application/pdf",
    )

    headers = captured[0]["headers"]
    assert headers["x-ms-blob-type"] == "BlockBlob"
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Content-Length"] == "3"
    # MUST NOT send Bearer or data-partition-id (SAS is the auth).
    assert "Authorization" not in headers
    assert "data-partition-id" not in headers
    assert captured[0]["allow_redirects"] is False


def test_upload_file_bytes_default_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "put",
        lambda **_: _FakeResponse(status_code=201),
    )
    upload_file_bytes("https://blob.example/sas", b"abc")
    assert captured[0]["headers"]["Content-Type"] == "application/octet-stream"


def test_upload_file_bytes_200_is_treated_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only HTTP 201 is success on Azure Put Blob; 200 is unexpected."""
    _patch_method(
        monkeypatch,
        "put",
        lambda **_: _FakeResponse(status_code=200, body="weird"),
    )
    result = upload_file_bytes("https://blob.example/sas", b"abc")
    assert result.ok is False
    assert result.http_status == 200
    assert result.bytes_uploaded == 0


def test_upload_file_bytes_403_expired_sas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_method(
        monkeypatch,
        "put",
        lambda **_: _FakeResponse(
            status_code=403,
            body="<Error><Code>AuthenticationFailed</Code></Error>",
        ),
    )
    result = upload_file_bytes("https://blob.example/sas", b"abc")
    assert result.ok is False
    assert result.http_status == 403
    assert result.bytes_uploaded == 0
    assert result.error_message is not None
    assert "AuthenticationFailed" in result.error_message


def test_upload_file_bytes_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_put(**_: Any) -> Any:
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(files_module.requests, "put", fake_put)
    result = upload_file_bytes(
        "https://blob.example/sas", b"abc", timeout=42
    )
    assert result.ok is False
    assert result.http_status is None
    assert result.error_message is not None
    assert "timed out" in result.error_message.lower()
    assert "42" in result.error_message
    assert result.bytes_uploaded == 0


def test_upload_file_bytes_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_put(**_: Any) -> Any:
        raise requests.exceptions.ConnectionError("reset")

    monkeypatch.setattr(files_module.requests, "put", fake_put)
    result = upload_file_bytes("https://blob.example/sas", b"abc")
    assert result.ok is False
    assert "ConnectionError" in (result.error_message or "")
    assert result.bytes_uploaded == 0


def test_upload_file_bytes_large_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "put",
        lambda **_: _FakeResponse(status_code=201),
    )
    payload = b"x" * (5 * 1024 * 1024)  # 5 MB — well within v1 limit
    result = upload_file_bytes("https://blob.example/sas", payload)
    assert result.ok is True
    assert result.bytes_uploaded == len(payload)
    assert captured[0]["headers"]["Content-Length"] == str(len(payload))


@pytest.mark.parametrize("signed_url", ["", "   ", "\t\n"])
def test_upload_file_bytes_rejects_blank_signed_url(signed_url: str) -> None:
    with pytest.raises(ValueError, match="non-empty signed_url"):
        upload_file_bytes(signed_url, b"abc")


def test_upload_file_bytes_rejects_empty_bytes() -> None:
    with pytest.raises(ValueError, match="non-empty bytes"):
        upload_file_bytes("https://blob/x", b"")


# ===========================================================================
# post_file_metadata
# ===========================================================================


def _md_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "file_source": "/abc/def",
        "file_id": "fid-1",
        "display_name": "well.las",
        "description": "",
        "legal_tag": "opendes-public-usa",
        "acl_owners": "data.default.owners@opendes.dataservices.energy",
        "acl_viewers": "data.default.viewers@opendes.dataservices.energy",
    }
    base.update(overrides)
    return base


def test_post_file_metadata_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "post",
        lambda **_: _FakeResponse(
            status_code=201,
            headers={"correlation-id": "corr-md-1"},
            json_payload={"id": "opendes:dataset--File.Generic:abc", "version": 17},
        ),
    )

    result = post_file_metadata(_connection(), token="t", **_md_kwargs())

    assert isinstance(result, FileMetadataResult)
    assert result.ok is True
    assert result.http_status == 201
    assert result.record_id == "opendes:dataset--File.Generic:abc"
    assert result.record_version == 17
    assert result.correlation_id == "corr-md-1"
    assert captured[0]["url"].endswith(FILES_METADATA_PATH)


def test_post_file_metadata_body_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "post",
        lambda **_: _FakeResponse(
            status_code=201, json_payload={"id": "r", "version": 1}
        ),
    )

    post_file_metadata(
        _connection(),
        token="t",
        **_md_kwargs(description="my file"),
    )
    body = captured[0]["json"]

    # Kind is the schema authority literal (NOT partition-prefixed).
    assert body["kind"] == "osdu:wks:dataset--File.Generic:1.0.0"
    # Legal block present + compliant status.
    assert body["legal"]["legaltags"] == ["opendes-public-usa"]
    assert body["legal"]["status"] == "compliant"
    assert body["legal"]["otherRelevantDataCountries"] == ["US"]
    # ACL block.
    assert body["acl"]["owners"] == [
        "data.default.owners@opendes.dataservices.energy"
    ]
    assert body["acl"]["viewers"] == [
        "data.default.viewers@opendes.dataservices.energy"
    ]
    # FileSource is in the right path.
    assert (
        body["data"]["DatasetProperties"]["FileSourceInfo"]["FileSource"]
        == "/abc/def"
    )
    assert body["data"]["Name"] == "well.las"
    assert body["data"]["Description"] == "my file"


def test_post_file_metadata_omits_description_when_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "post",
        lambda **_: _FakeResponse(
            status_code=201, json_payload={"id": "r"}
        ),
    )
    post_file_metadata(
        _connection(), token="t", **_md_kwargs(description="   ")
    )
    assert "Description" not in captured[0]["json"]["data"]


def test_post_file_metadata_merges_extra_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "post",
        lambda **_: _FakeResponse(
            status_code=201, json_payload={"id": "r", "version": 1}
        ),
    )

    post_file_metadata(
        _connection(),
        token="t",
        **_md_kwargs(
            extra_data={
                "SchemaFormatTypeID": "osdu:reference-data--SchemaFormatType:LAS2:",
                "ResourceSecurityClassification": (
                    "osdu:reference-data--ResourceSecurityClassification:RESTRICTED:"
                ),
                # These must NOT clobber the canonical fields.
                "Name": "SHOULD_BE_IGNORED",
                "DatasetProperties": {"FileSourceInfo": {"FileSource": "bad"}},
            }
        ),
    )
    data = captured[0]["json"]["data"]

    # Rich fields carried through.
    assert (
        data["SchemaFormatTypeID"]
        == "osdu:reference-data--SchemaFormatType:LAS2:"
    )
    assert data["ResourceSecurityClassification"].endswith("RESTRICTED:")
    # Canonical Name + FileSource win over extra_data.
    assert data["Name"] == "well.las"
    assert (
        data["DatasetProperties"]["FileSourceInfo"]["FileSource"] == "/abc/def"
    )


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 500])
def test_post_file_metadata_http_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _patch_method(
        monkeypatch,
        "post",
        lambda **_: _FakeResponse(
            status_code=status_code,
            headers={"correlation-id": f"corr-{status_code}"},
            json_payload={"message": f"boom {status_code}"},
        ),
    )
    result = post_file_metadata(_connection(), token="t", **_md_kwargs())
    assert result.ok is False
    assert result.http_status == status_code
    assert result.correlation_id == f"corr-{status_code}"
    assert result.error_message is not None
    assert f"boom {status_code}" in result.error_message
    assert result.record_id is None


def test_post_file_metadata_400_bad_legal_tag_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad legal tag must surface the server message."""
    _patch_method(
        monkeypatch,
        "post",
        lambda **_: _FakeResponse(
            status_code=400,
            json_payload={"message": "Legal tag is not valid for partition"},
        ),
    )
    result = post_file_metadata(_connection(), token="t", **_md_kwargs())
    assert result.ok is False
    assert result.http_status == 400
    assert "Legal tag" in (result.error_message or "")


def test_post_file_metadata_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(files_module.requests, "post", fake_post)
    result = post_file_metadata(_connection(), token="t", **_md_kwargs())
    assert result.ok is False
    assert result.http_status is None
    assert "timed out" in (result.error_message or "").lower()


def test_post_file_metadata_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.ConnectionError("dns")

    monkeypatch.setattr(files_module.requests, "post", fake_post)
    result = post_file_metadata(_connection(), token="t", **_md_kwargs())
    assert result.ok is False
    assert "ConnectionError" in (result.error_message or "")


def test_post_file_metadata_outgoing_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_method(
        monkeypatch,
        "post",
        lambda **_: _FakeResponse(
            status_code=201, json_payload={"id": "r"}
        ),
    )
    post_file_metadata(_connection(), token="bearer-abc", **_md_kwargs())
    headers = captured[0]["headers"]
    assert headers["Authorization"] == "Bearer bearer-abc"
    assert headers["data-partition-id"] == "example-opendes"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    assert captured[0]["timeout"] == FILES_TIMEOUT_SECONDS
    assert captured[0]["allow_redirects"] is False


@pytest.mark.parametrize(
    "header_name",
    ["correlation-id", "X-Correlation-ID", "Request-Id", "X-Request-Id"],
)
def test_post_file_metadata_correlation_id_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, header_name: str
) -> None:
    _patch_method(
        monkeypatch,
        "post",
        lambda **_: _FakeResponse(
            status_code=201,
            headers={header_name: "corr-x"},
            json_payload={"id": "r"},
        ),
    )
    result = post_file_metadata(_connection(), token="t", **_md_kwargs())
    assert result.correlation_id == "corr-x"


# ===========================================================================
# get_download_url / check_file_blob
# ===========================================================================


def test_get_download_url_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_method(
        monkeypatch,
        "get",
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={"SignedUrl": "https://blob.example/sas?sig=z"},
        ),
    )
    res = get_download_url(
        _connection(), "t", "opendes:dataset--File.Generic:abc:"
    )
    assert res.ok is True
    assert res.signed_url == "https://blob.example/sas?sig=z"
    # trailing version marker stripped from the record id in the path
    assert captured[0]["url"].endswith(
        "/api/file/v2/files/opendes:dataset--File.Generic:abc/downloadURL"
    )


def test_get_download_url_missing_signed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_method(
        monkeypatch, "get",
        lambda **_: _FakeResponse(status_code=200, json_payload={}),
    )
    res = get_download_url(_connection(), "t", "opendes:x:1")
    assert res.ok is False
    assert "SignedUrl" in (res.error_message or "")


def test_check_file_blob_present(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(**kwargs: Any) -> _FakeResponse:
        if str(kwargs.get("url", "")).endswith("/downloadURL"):
            return _FakeResponse(
                status_code=200,
                json_payload={"SignedUrl": "https://blob.example/sas"},
            )
        # the SAS blob GET
        return _FakeResponse(status_code=206, headers={"Content-Length": "42"})

    _patch_method(monkeypatch, "get", factory)
    res = check_file_blob(_connection(), "t", "opendes:file:1")
    assert res.present is True
    assert res.blob_http_status == 206
    assert res.content_length == 42


def test_check_file_blob_missing_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(**kwargs: Any) -> _FakeResponse:
        if str(kwargs.get("url", "")).endswith("/downloadURL"):
            return _FakeResponse(
                status_code=200,
                json_payload={"SignedUrl": "https://blob.example/sas"},
            )
        return _FakeResponse(status_code=404, body="BlobNotFound")

    _patch_method(monkeypatch, "get", factory)
    res = check_file_blob(_connection(), "t", "opendes:file:1")
    assert res.present is False
    assert res.blob_http_status == 404


def test_check_file_blob_download_url_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_method(
        monkeypatch, "get",
        lambda **_: _FakeResponse(status_code=404, body="not found"),
    )
    res = check_file_blob(_connection(), "t", "opendes:file:1")
    assert res.present is False
    assert res.blob_http_status is None



@pytest.mark.parametrize("token", ["", "   ", "\t\n"])
def test_post_file_metadata_rejects_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="non-empty bearer token"):
        post_file_metadata(_connection(), token=token, **_md_kwargs())


@pytest.mark.parametrize(
    "field_name",
    ["file_source", "display_name", "legal_tag", "acl_owners", "acl_viewers"],
)
@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_post_file_metadata_rejects_blank_required_fields(
    field_name: str, blank: str
) -> None:
    with pytest.raises(ValueError, match=f"non-empty {field_name}"):
        post_file_metadata(
            _connection(), token="t", **_md_kwargs(**{field_name: blank})
        )


# ===========================================================================
# upload_file_blocks (streaming block-blob upload for large files)
# ===========================================================================


def test_upload_file_blocks_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _patch_method(
        monkeypatch, "put", lambda **_: _FakeResponse(status_code=201)
    )
    path = tmp_path / "big.segy"
    path.write_bytes(b"ABCDE")  # 5 bytes → 3 blocks at size 2

    result = upload_file_blocks(
        "https://blob.example/sas?sig=x",
        path,
        content_type="application/x-segy",
        block_size=2,
    )

    assert result.ok is True
    assert result.http_status == 201
    assert result.bytes_uploaded == 5
    urls = [c["url"] for c in captured]
    block_puts = [u for u in urls if "comp=block&" in u]
    commits = [u for u in urls if u.endswith("comp=blocklist")]
    assert len(block_puts) == 3
    assert len(commits) == 1
    commit = next(c for c in captured if c["url"].endswith("comp=blocklist"))
    assert commit["headers"]["x-ms-blob-content-type"] == "application/x-segy"
    assert commit["data"].decode("utf-8").count("<Latest>") == 3


def test_upload_file_blocks_block_failure_aborts_before_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def factory(**kwargs: Any) -> _FakeResponse:
        calls["n"] += 1
        return _FakeResponse(
            status_code=500 if calls["n"] == 2 else 201, body="boom"
        )

    captured = _patch_method(monkeypatch, "put", factory)
    path = tmp_path / "big.segy"
    path.write_bytes(b"ABCD")  # 2 blocks at size 2

    result = upload_file_blocks("https://blob.example/sas", path, block_size=2)

    assert result.ok is False
    assert result.http_status == 500
    assert not any(c["url"].endswith("comp=blocklist") for c in captured)


def test_upload_file_blocks_commit_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def factory(**kwargs: Any) -> _FakeResponse:
        if kwargs["url"].endswith("comp=blocklist"):
            return _FakeResponse(status_code=500, body="commit boom")
        return _FakeResponse(status_code=201)

    _patch_method(monkeypatch, "put", factory)
    path = tmp_path / "f"
    path.write_bytes(b"ABCD")

    result = upload_file_blocks("https://blob.example/sas", path, block_size=2)

    assert result.ok is False
    assert result.http_status == 500


def test_upload_file_blocks_empty_file_no_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _patch_method(
        monkeypatch, "put", lambda **_: _FakeResponse(status_code=201)
    )
    path = tmp_path / "empty"
    path.write_bytes(b"")

    result = upload_file_blocks("https://blob.example/sas", path)

    assert result.ok is False
    assert "empty" in (result.error_message or "").lower()
    assert captured == []


def test_upload_file_blocks_block_ids_fixed_length_and_ordered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _patch_method(
        monkeypatch, "put", lambda **_: _FakeResponse(status_code=201)
    )
    path = tmp_path / "f"
    path.write_bytes(b"ABCDEF")  # 3 blocks at size 2
    upload_file_blocks("https://blob.example/sas", path, block_size=2)

    commit = next(c for c in captured if c["url"].endswith("comp=blocklist"))
    ids = re.findall(r"<Latest>([^<]+)</Latest>", commit["data"].decode("utf-8"))
    assert len(ids) == 3
    assert len({len(i) for i in ids}) == 1  # Azure requires equal-length ids
    decoded = [base64.b64decode(i).decode("ascii") for i in ids]
    assert decoded == ["00000000", "00000001", "00000002"]


@pytest.mark.parametrize("bad_url", ["", "   "])
def test_upload_file_blocks_rejects_blank_url(
    bad_url: str, tmp_path: Path
) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    with pytest.raises(ValueError, match="non-empty signed_url"):
        upload_file_blocks(bad_url, path)


def test_upload_file_blocks_rejects_nonpositive_block_size(
    tmp_path: Path,
) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    with pytest.raises(ValueError, match="positive integer"):
        upload_file_blocks("https://blob.example/sas", path, block_size=0)

