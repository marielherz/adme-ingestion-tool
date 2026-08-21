"""ADME File Service (v2) — three-call upload flow.

Implements the canonical Microsoft ADME File Service v2 sequence:

1. ``GET  /api/file/v2/files/uploadURL`` — allocate a SAS-signed Azure
   Blob staging URL plus an opaque ``FileSource`` token.
2. ``PUT  <signed_url>`` — push the raw bytes directly to Azure Blob
   Storage. The SAS query string IS the auth; **no Bearer, no
   ``data-partition-id``** on this call.
3. ``POST /api/file/v2/files/metadata`` — register an
   ``osdu:wks:dataset--File.Generic:1.0.0`` Storage record that points
   at the staged ``FileSource``.

Sibling to :mod:`app.services.legal_tags` and :mod:`app.services.ingestion`:
stdlib + ``requests`` only, per-call timeouts, no internal retries, and
each public function returns a frozen result dataclass from
:mod:`app.models.osdu`. The Azure PUT does NOT go through
``_call_files`` — it has its own minimal try/except (different host,
different headers, different success code, no correlation header).

Authoritative API research lives in
``.squad/decisions/inbox/darryl-file-upload-api.md``; the public
contract lives in
``.squad/decisions/inbox/satya-file-upload-page-contract.md``.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from urllib.parse import quote

import requests  # type: ignore[import-untyped]

from app.models.connection import ADMEConnection
from app.models.osdu import (
    BlobProbeResult,
    DownloadURLResult,
    FileMetadataResult,
    UploadBytesResult,
    UploadURLResult,
)

FILES_UPLOAD_URL_PATH = "/api/file/v2/files/uploadURL"
FILES_METADATA_PATH = "/api/file/v2/files/metadata"
FILES_DOWNLOAD_URL_PATH_TEMPLATE = "/api/file/v2/files/{record_id}/downloadURL"
FILES_TIMEOUT_SECONDS = 15
MAX_FILE_BYTES_V1 = 100 * 1024 * 1024
FILE_GENERIC_KIND = "osdu:wks:dataset--File.Generic:1.0.0"

# Above this size, a single Put Blob is impractical (memory + service limits),
# so upload as a Block Blob in chunks. Seismic (SEG-Y) volumes need this.
FILES_BLOCK_UPLOAD_THRESHOLD_BYTES = 128 * 1024 * 1024
FILES_BLOCK_SIZE_BYTES = 16 * 1024 * 1024
FILES_LARGE_UPLOAD_TIMEOUT_SECONDS = 600
FILES_BLOCK_MAX_RETRIES = 4
FILES_BLOCK_RETRY_BACKOFF_SECONDS = 2.0

_CORRELATION_HEADER_NAMES: tuple[str, ...] = (
    "correlation-id",
    "x-correlation-id",
    "request-id",
    "x-request-id",
)

_ERROR_BODY_TEXT_LIMIT = 500

__all__ = [
    "FILE_GENERIC_KIND",
    "FILES_BLOCK_SIZE_BYTES",
    "FILES_BLOCK_MAX_RETRIES",
    "FILES_BLOCK_RETRY_BACKOFF_SECONDS",
    "FILES_BLOCK_UPLOAD_THRESHOLD_BYTES",
    "FILES_DOWNLOAD_URL_PATH_TEMPLATE",
    "FILES_LARGE_UPLOAD_TIMEOUT_SECONDS",
    "FILES_METADATA_PATH",
    "FILES_TIMEOUT_SECONDS",
    "FILES_UPLOAD_URL_PATH",
    "MAX_FILE_BYTES_V1",
    "check_file_blob",
    "get_download_url",
    "get_upload_url",
    "post_file_metadata",
    "upload_file_blocks",
    "upload_file_bytes",
]


def get_upload_url(
    connection: ADMEConnection,
    token: str,
) -> UploadURLResult:
    """``GET /api/file/v2/files/uploadURL``.

    Per Darryl's research the ADME response is a flat
    ``{"SignedURL": ..., "FileSource": ...}`` object, but older OSDU
    prototype builds wrapped the same fields under a ``Location``
    envelope. Read defensively: probe ``body.get("Location", body)``
    then pull ``SignedURL`` / ``FileSource``. ``FileID`` is optional
    and surfaced verbatim when present.
    """
    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_files(
            connection=connection,
            token=token,
            method="GET",
            path=FILES_UPLOAD_URL_PATH,
            json_body=None,
        )
    )

    if http_status is None:
        return UploadURLResult(
            ok=False,
            http_status=None,
            latency_ms=latency_ms,
            correlation_id=None,
            error_message=error_message,
        )

    if 200 <= http_status < 300:
        body = parsed_body if isinstance(parsed_body, dict) else {}
        # Defensive: some legacy responses nest the fields under
        # "Location". Microsoft Learn shows them flat. Try the envelope
        # first, fall back to the root.
        location = body.get("Location")
        source: dict[str, Any] = (
            location if isinstance(location, dict) else body
        )

        signed_url = _coerce_str(source.get("SignedURL"))
        file_source = _coerce_str(source.get("FileSource"))
        file_id = _coerce_str(source.get("FileID"))

        if not signed_url or not file_source:
            return UploadURLResult(
                ok=False,
                http_status=http_status,
                latency_ms=latency_ms,
                correlation_id=correlation_id,
                error_message=(
                    "uploadURL response missing required SignedURL or "
                    "FileSource field."
                ),
            )

        return UploadURLResult(
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            error_message=None,
            signed_url=signed_url,
            file_source=file_source,
            file_id=file_id,
        )

    return UploadURLResult(
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        correlation_id=correlation_id,
        error_message=error_message,
    )


def get_download_url(
    connection: ADMEConnection,
    token: str,
    record_id: str,
) -> DownloadURLResult:
    """``GET /api/file/v2/files/{id}/downloadURL``.

    Returns a SAS-signed Azure Blob URL for the File.Generic record's bulk
    bytes. Any trailing version marker (``:``) on ``record_id`` is stripped.
    The response field is ``SignedUrl`` (probed case-insensitively for
    resilience across ADME builds).
    """
    if not record_id or not record_id.strip():
        raise ValueError("A non-empty record_id is required for get_download_url.")

    rid = record_id.rstrip(":")
    path = FILES_DOWNLOAD_URL_PATH_TEMPLATE.format(record_id=quote(rid, safe=":"))
    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_files(
            connection=connection,
            token=token,
            method="GET",
            path=path,
            json_body=None,
        )
    )

    if http_status is not None and 200 <= http_status < 300:
        body = parsed_body if isinstance(parsed_body, dict) else {}
        signed = (
            body.get("SignedUrl")
            or body.get("SignedURL")
            or body.get("signedUrl")
        )
        signed_url = _coerce_str(signed)
        if not signed_url:
            return DownloadURLResult(
                ok=False,
                http_status=http_status,
                latency_ms=latency_ms,
                correlation_id=correlation_id,
                error_message="downloadURL response missing SignedUrl.",
            )
        return DownloadURLResult(
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            signed_url=signed_url,
        )

    return DownloadURLResult(
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        correlation_id=correlation_id,
        error_message=error_message,
    )


def check_file_blob(
    connection: ADMEConnection,
    token: str,
    record_id: str,
    *,
    timeout: int = FILES_TIMEOUT_SECONDS,
) -> BlobProbeResult:
    """Confirm a File.Generic record's bulk blob is actually retrievable.

    Resolves the record's download URL then issues a tiny ranged GET
    (``bytes=0-0``) against the SAS URL. A metadata record can exist while
    its blob is missing/purged (the exact failure this whole feature
    guards against), so ``present`` is ``True`` only on a 200/206 from the
    blob store. Network/transport failures are returned as ``present=False``
    with an ``error_message`` — never raised.
    """
    du = get_download_url(connection, token, record_id)
    if not du.ok or not du.signed_url:
        return BlobProbeResult(
            record_id=record_id,
            present=False,
            download_http_status=du.http_status,
            error_message=du.error_message or "Could not resolve download URL.",
        )
    try:
        resp = requests.get(
            url=du.signed_url,
            headers={"Range": "bytes=0-0"},
            timeout=timeout,
            stream=True,
        )
    except requests.RequestException as exc:
        return BlobProbeResult(
            record_id=record_id,
            present=False,
            download_http_status=du.http_status,
            error_message=f"{type(exc).__name__}: {exc}",
        )
    finally:
        try:
            resp.close()  # type: ignore[has-type]
        except Exception:  # pragma: no cover - defensive
            pass

    present = resp.status_code in (200, 206)
    length = _coerce_int(resp.headers.get("Content-Length"))
    return BlobProbeResult(
        record_id=record_id,
        present=present,
        download_http_status=du.http_status,
        blob_http_status=resp.status_code,
        content_length=length,
        error_message=None if present else f"blob HTTP {resp.status_code}",
    )


def upload_file_bytes(
    signed_url: str,
    file_bytes: bytes,
    *,
    content_type: str = "application/octet-stream",
    timeout: int = 120,
) -> UploadBytesResult:
    """``PUT <signed_url>`` with raw bytes to Azure Blob Storage.

    This is the only call in the codebase that does not go through
    ``_call_*``. It hits Azure Blob Storage directly via the SAS-signed
    URL returned by :func:`get_upload_url`. There is no ADME correlation
    id on this response — :class:`UploadBytesResult` has no
    ``correlation_id`` field by design.

    ``x-ms-blob-type: BlockBlob`` is mandatory on Azure Put Blob.
    ``Authorization`` and ``data-partition-id`` MUST NOT be sent — the
    SAS query string is the auth; a Bearer header here causes Azure to
    return ``403 AuthenticationFailed``.

    Success is HTTP 201. Any other status is treated as failure with
    ``bytes_uploaded = 0``.
    """
    if not signed_url or not signed_url.strip():
        raise ValueError(
            "A non-empty signed_url is required for upload_file_bytes."
        )
    if not file_bytes:
        raise ValueError(
            "file_bytes must be a non-empty bytes object for "
            "upload_file_bytes."
        )

    size = len(file_bytes)
    headers = {
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": content_type or "application/octet-stream",
        "Content-Length": str(size),
    }

    started_at = perf_counter()

    try:
        response = requests.put(
            url=signed_url,
            data=file_bytes,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.Timeout:
        return UploadBytesResult(
            ok=False,
            http_status=None,
            latency_ms=_elapsed_ms(started_at),
            error_message=f"Request timed out after {timeout}s",
            bytes_uploaded=0,
        )
    except requests.RequestException as exc:
        return UploadBytesResult(
            ok=False,
            http_status=None,
            latency_ms=_elapsed_ms(started_at),
            error_message=f"{type(exc).__name__}: {exc}",
            bytes_uploaded=0,
        )
    except Exception as exc:  # pragma: no cover - defensive boundary
        return UploadBytesResult(
            ok=False,
            http_status=None,
            latency_ms=_elapsed_ms(started_at),
            error_message=f"{type(exc).__name__}: {exc}",
            bytes_uploaded=0,
        )

    latency_ms = _elapsed_ms(started_at)
    status_code = response.status_code

    if status_code == 201:
        return UploadBytesResult(
            ok=True,
            http_status=status_code,
            latency_ms=latency_ms,
            error_message=None,
            bytes_uploaded=size,
        )

    body_text = getattr(response, "text", "") or ""
    error_message = (
        _truncate(body_text) if body_text else f"HTTP {status_code}"
    )
    return UploadBytesResult(
        ok=False,
        http_status=status_code,
        latency_ms=latency_ms,
        error_message=error_message,
        bytes_uploaded=0,
    )


def _block_id(index: int) -> str:
    """Return a fixed-length, URL-safe base64 block id for ``index``.

    Azure requires every block id in a blob to be the same byte length; an
    8-digit zero-padded index encodes to a constant 12-char base64 string
    (supports up to 100M blocks).
    """
    return base64.b64encode(f"{index:08d}".encode("ascii")).decode("ascii")


def _build_block_list_xml(block_ids: list[str]) -> bytes:
    latest = "".join(f"<Latest>{block_id}</Latest>" for block_id in block_ids)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<BlockList>{latest}</BlockList>"
    ).encode()


def upload_file_blocks(
    signed_url: str,
    file_path: Path,
    *,
    content_type: str = "application/octet-stream",
    block_size: int = FILES_BLOCK_SIZE_BYTES,
    timeout: int = FILES_LARGE_UPLOAD_TIMEOUT_SECONDS,
) -> UploadBytesResult:
    """Stream a file to Azure Blob Storage as a Block Blob in chunks.

    Reads ``file_path`` in ``block_size`` pieces, ``PUT``s each as a staged
    block (``comp=block``), then commits the ordered block list
    (``comp=blocklist``). This avoids loading the whole file into memory and
    bypasses the single Put-Blob size ceiling — required for large seismic
    (SEG-Y) volumes.

    Auth is the SAS query string on ``signed_url`` (no Bearer / partition
    header, like :func:`upload_file_bytes`). Success is HTTP 201 on the
    block-list commit; a failure on any block aborts with ``ok=False``.
    """
    if not signed_url or not signed_url.strip():
        raise ValueError(
            "A non-empty signed_url is required for upload_file_blocks."
        )
    if block_size <= 0:
        raise ValueError("block_size must be a positive integer.")

    started_at = perf_counter()
    block_ids: list[str] = []
    total = 0

    try:
        with file_path.open("rb") as handle:
            index = 0
            while True:
                chunk = handle.read(block_size)
                if not chunk:
                    break
                block_id = _block_id(index)
                block_ids.append(block_id)
                total += len(chunk)
                put_url = (
                    f"{signed_url}&comp=block"
                    f"&blockid={quote(block_id, safe='')}"
                )
                response = None
                last_error: Exception | None = None
                for attempt in range(FILES_BLOCK_MAX_RETRIES + 1):
                    try:
                        response = requests.put(
                            url=put_url,
                            data=chunk,
                            headers={"Content-Length": str(len(chunk))},
                            timeout=timeout,
                            allow_redirects=False,
                        )
                        break
                    except requests.RequestException as exc:
                        last_error = exc
                    if attempt < FILES_BLOCK_MAX_RETRIES:
                        sleep(FILES_BLOCK_RETRY_BACKOFF_SECONDS * (attempt + 1))
                if response is None:
                    return UploadBytesResult(
                        ok=False,
                        http_status=None,
                        latency_ms=_elapsed_ms(started_at),
                        error_message=(
                            f"Block {index} failed after retries: {last_error}"
                        ),
                        bytes_uploaded=0,
                    )
                if response.status_code != 201:
                    body = getattr(response, "text", "") or ""
                    return UploadBytesResult(
                        ok=False,
                        http_status=response.status_code,
                        latency_ms=_elapsed_ms(started_at),
                        error_message=(
                            _truncate(body)
                            if body
                            else f"Block {index} failed: "
                            f"HTTP {response.status_code}"
                        ),
                        bytes_uploaded=0,
                    )
                index += 1

        if not block_ids:
            return UploadBytesResult(
                ok=False,
                http_status=None,
                latency_ms=_elapsed_ms(started_at),
                error_message="File is empty; nothing to upload.",
                bytes_uploaded=0,
            )

        commit = requests.put(
            url=f"{signed_url}&comp=blocklist",
            data=_build_block_list_xml(block_ids),
            headers={
                "Content-Type": "application/xml",
                "x-ms-blob-content-type": (
                    content_type or "application/octet-stream"
                ),
            },
            timeout=timeout,
            allow_redirects=False,
        )
    except OSError as exc:
        return UploadBytesResult(
            ok=False,
            http_status=None,
            latency_ms=_elapsed_ms(started_at),
            error_message=f"Cannot read {file_path.name}: {exc}",
            bytes_uploaded=0,
        )
    except requests.Timeout:
        return UploadBytesResult(
            ok=False,
            http_status=None,
            latency_ms=_elapsed_ms(started_at),
            error_message=f"Request timed out after {timeout}s",
            bytes_uploaded=0,
        )
    except requests.RequestException as exc:
        return UploadBytesResult(
            ok=False,
            http_status=None,
            latency_ms=_elapsed_ms(started_at),
            error_message=f"{type(exc).__name__}: {exc}",
            bytes_uploaded=0,
        )

    if commit.status_code == 201:
        return UploadBytesResult(
            ok=True,
            http_status=201,
            latency_ms=_elapsed_ms(started_at),
            error_message=None,
            bytes_uploaded=total,
        )
    body = getattr(commit, "text", "") or ""
    return UploadBytesResult(
        ok=False,
        http_status=commit.status_code,
        latency_ms=_elapsed_ms(started_at),
        error_message=(
            _truncate(body)
            if body
            else f"Block-list commit failed: HTTP {commit.status_code}"
        ),
        bytes_uploaded=0,
    )


def post_file_metadata(
    connection: ADMEConnection,
    token: str,
    *,
    file_source: str,
    file_id: str,
    display_name: str,
    description: str,
    legal_tag: str,
    acl_owners: str,
    acl_viewers: str,
    extra_data: Mapping[str, Any] | None = None,
) -> FileMetadataResult:
    """``POST /api/file/v2/files/metadata``.

    Builds the canonical ``osdu:wks:dataset--File.Generic:1.0.0`` record
    per Darryl's research, embedding ``file_source`` into
    ``data.DatasetProperties.FileSourceInfo.FileSource``. The ``kind``
    is the literal string ``"osdu:wks:dataset--File.Generic:1.0.0"`` —
    NOT partition-prefixed (kinds use the schema authority, record IDs
    use the partition prefix).

    ``description`` is optional; when blank it is omitted from the body.
    ``file_id`` is accepted for parity with the upload-URL response but
    is not required by the metadata POST itself; ADME mints the record
    id server-side.

    ``extra_data`` merges additional ``data`` fields (e.g.
    ``SchemaFormatTypeID``, ``ResourceSecurityClassification``) into the
    record so pre-generated dataset metadata is not lost. The canonical
    ``Name`` and ``DatasetProperties`` always win over ``extra_data``.
    """
    if not file_source or not file_source.strip():
        raise ValueError(
            "A non-empty file_source is required for post_file_metadata."
        )
    if not display_name or not display_name.strip():
        raise ValueError(
            "A non-empty display_name is required for post_file_metadata."
        )
    if not legal_tag or not legal_tag.strip():
        raise ValueError(
            "A non-empty legal_tag is required for post_file_metadata."
        )
    if not acl_owners or not acl_owners.strip():
        raise ValueError(
            "A non-empty acl_owners is required for post_file_metadata."
        )
    if not acl_viewers or not acl_viewers.strip():
        raise ValueError(
            "A non-empty acl_viewers is required for post_file_metadata."
        )

    file_source_info: dict[str, Any] = {
        "FileSource": file_source,
        "Name": display_name,
    }
    data_block: dict[str, Any] = {
        key: value
        for key, value in (extra_data or {}).items()
        if key not in ("Name", "DatasetProperties")
    }
    data_block["Name"] = display_name
    data_block["DatasetProperties"] = {"FileSourceInfo": file_source_info}
    if description and description.strip():
        data_block["Description"] = description

    body = {
        "kind": FILE_GENERIC_KIND,
        "acl": {
            "owners": [acl_owners],
            "viewers": [acl_viewers],
        },
        "legal": {
            "legaltags": [legal_tag],
            "otherRelevantDataCountries": ["US"],
            "status": "compliant",
        },
        "data": data_block,
    }
    # file_id is informational only; surface it back to the caller via
    # the result rather than embedding in the request.
    _ = file_id

    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_files(
            connection=connection,
            token=token,
            method="POST",
            path=FILES_METADATA_PATH,
            json_body=body,
        )
    )

    if http_status is None:
        return FileMetadataResult(
            ok=False,
            http_status=None,
            latency_ms=latency_ms,
            correlation_id=None,
            error_message=error_message,
        )

    if 200 <= http_status < 300:
        result_body = parsed_body if isinstance(parsed_body, dict) else {}
        record_id = _coerce_str(result_body.get("id"))
        record_version = _coerce_int(result_body.get("version"))
        return FileMetadataResult(
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            error_message=None,
            record_id=record_id,
            record_version=record_version,
        )

    return FileMetadataResult(
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        correlation_id=correlation_id,
        error_message=error_message,
    )


# --- internal helpers ---------------------------------------------------


def _call_files(
    connection: ADMEConnection,
    token: str,
    *,
    method: str,
    path: str,
    json_body: dict | None,
) -> tuple[dict | str | None, int | None, str | None, float, str | None]:
    """Mirror of :func:`app.services.legal_tags._call_legal`.

    Bearer + ``data-partition-id`` + ``Accept: application/json`` on
    every call; ``Content-Type: application/json`` added when a JSON
    body is supplied. 15s timeout. Returns the same
    ``(parsed_body, http_status, correlation_id, latency_ms,
    error_message)`` tuple shape as ``_call_legal`` so test fixtures
    port over.
    """
    if not connection.is_valid():
        raise ValueError(
            "ADME connection is incomplete. Endpoint, tenant ID, "
            "client ID, and data partition ID are required. Service "
            "principal auth also requires a client secret."
        )
    if not token or not token.strip():
        raise ValueError(
            "A non-empty bearer token is required for file service calls."
        )

    url = f"{connection.endpoint.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "data-partition-id": connection.data_partition_id,
        "Accept": "application/json",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    started_at = perf_counter()

    try:
        if method == "GET":
            response = requests.get(
                url=url,
                headers=headers,
                timeout=FILES_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        elif method == "POST":
            response = requests.post(
                url=url,
                headers=headers,
                json=json_body,
                timeout=FILES_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
    except requests.Timeout:
        return (
            None,
            None,
            None,
            _elapsed_ms(started_at),
            f"Request timed out after {FILES_TIMEOUT_SECONDS}s",
        )
    except requests.RequestException as exc:
        return (
            None,
            None,
            None,
            _elapsed_ms(started_at),
            f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # pragma: no cover - defensive boundary
        return (
            None,
            None,
            None,
            _elapsed_ms(started_at),
            f"{type(exc).__name__}: {exc}",
        )

    latency_ms = _elapsed_ms(started_at)
    correlation_id = _extract_correlation_id(
        response.headers, _CORRELATION_HEADER_NAMES
    )
    parsed_body = _try_parse_json(response)

    if 200 <= response.status_code < 300:
        return (
            parsed_body,
            response.status_code,
            correlation_id,
            latency_ms,
            None,
        )

    if parsed_body is not None and isinstance(parsed_body, dict):
        error_message = _error_message_from_json(
            parsed_body, response.status_code
        )
        return (
            parsed_body,
            response.status_code,
            correlation_id,
            latency_ms,
            error_message,
        )

    text = getattr(response, "text", "") or ""
    body: dict | str | None = text if text else None
    error_message = (
        _truncate(text) if text else f"HTTP {response.status_code}"
    )
    return (
        body,
        response.status_code,
        correlation_id,
        latency_ms,
        error_message,
    )


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 2)


def _extract_correlation_id(
    headers: object,
    candidate_names: Iterable[str],
) -> str | None:
    if headers is None:
        return None
    lowercase_lookup: dict[str, str] = {}
    try:
        items = headers.items()  # type: ignore[attr-defined]
    except AttributeError:
        return None
    for key, value in items:
        if isinstance(key, str) and isinstance(value, str):
            lowercase_lookup[key.lower()] = value
    for name in candidate_names:
        value = lowercase_lookup.get(name.lower())
        if value:
            return value
    return None


def _try_parse_json(response: requests.Response) -> dict | None:
    response_json = getattr(response, "json", None)
    if not callable(response_json):
        return None
    try:
        payload = response_json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _error_message_from_json(payload: dict, status_code: int) -> str:
    for key in ("message", "detail", "error", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value)

    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return _truncate(str(errors[0]))
    if isinstance(errors, dict) and errors:
        first_value = next(iter(errors.values()))
        return _truncate(str(first_value))

    return f"HTTP {status_code}"


def _truncate(value: str, limit: int = _ERROR_BODY_TEXT_LIMIT) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3]}..."


def _coerce_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None
