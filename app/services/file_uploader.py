"""Bulk file upload — stream the three-call File Service flow per file.

Builds on the single-file primitives in :mod:`app.services.files`
(:func:`get_upload_url`, :func:`upload_file_bytes`,
:func:`post_file_metadata`) to register many local files as
``osdu:wks:dataset--File.Generic`` Storage records, yielding one
:class:`FileUploadOutcome` per file.

Sequential — one file at a time, mirroring
:func:`app.services.bulk_loader.submit_tier`. A failure on one file
yields an error outcome and the loop continues to the next (no
abort-on-error policy at the service layer; the page can stop consuming
the iterator).

Per-load independence for the Smart Tier plan is automatic here: each
upload allocates a fresh Azure Blob staging URL and ADME mints a new
record id, so re-running the same files for a later load produces
physically separate blobs and records that age on their own tier clock.
The minted ``record_id`` / ``record_version`` on each outcome are what a
work-product manifest must reference to wire the file in.
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.models.connection import ADMEConnection
from app.models.osdu import FileUploadOutcome
from app.services.files import (
    get_upload_url,
    post_file_metadata,
    upload_file_bytes,
)

logger = logging.getLogger(__name__)

DEFAULT_CONTENT_TYPE = "application/octet-stream"
# Work-product blobs (SEG-Y, LAS) can be large; give the Azure PUT a
# generous ceiling. The three ADME calls keep the 15s default in files.py.
UPLOAD_BYTES_TIMEOUT_SECONDS = 300

__all__ = [
    "DEFAULT_CONTENT_TYPE",
    "UPLOAD_BYTES_TIMEOUT_SECONDS",
    "FileUploadItem",
    "guess_content_type",
    "upload_files",
]


@dataclass(frozen=True, slots=True)
class FileUploadItem:
    """One local file to upload and register.

    ``display_name`` falls back to the filename and ``content_type`` is
    guessed from the suffix when either is left blank. ``description`` is
    optional and omitted from the metadata record when empty.
    """

    path: Path
    display_name: str = ""
    description: str = ""
    content_type: str = ""


def guess_content_type(path: Path) -> str:
    """Best-effort MIME type from the filename, defaulting to octet-stream."""
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or DEFAULT_CONTENT_TYPE


def upload_files(
    connection: ADMEConnection,
    token: str,
    items: Sequence[FileUploadItem],
    *,
    legal_tag: str,
    acl_owners: str,
    acl_viewers: str,
    progress_callback: Callable[[FileUploadOutcome], None] | None = None,
) -> Iterator[FileUploadOutcome]:
    """Yield one :class:`FileUploadOutcome` per file in ``items``.

    Each file runs the full ``uploadURL -> PUT bytes -> metadata`` flow.
    The ACL/legal values stamp every registered record. ``items`` is
    consumed lazily so a caller can render progress as results stream in.
    """
    for item in items:
        outcome = _upload_one(
            connection,
            token,
            item,
            legal_tag=legal_tag,
            acl_owners=acl_owners,
            acl_viewers=acl_viewers,
        )
        if progress_callback is not None:
            try:
                progress_callback(outcome)
            except Exception:  # pragma: no cover - UI callback never fatal
                logger.exception("upload_files progress_callback failed")
        yield outcome


def _error_outcome(
    path: Path,
    message: str,
    *,
    bytes_uploaded: int = 0,
    file_source: str | None = None,
) -> FileUploadOutcome:
    return FileUploadOutcome(
        source_path=path,
        filename=path.name,
        status="error",
        record_id=None,
        record_version=None,
        file_source=file_source,
        bytes_uploaded=bytes_uploaded,
        error=message,
    )


def _upload_one(
    connection: ADMEConnection,
    token: str,
    item: FileUploadItem,
    *,
    legal_tag: str,
    acl_owners: str,
    acl_viewers: str,
) -> FileUploadOutcome:
    path = item.path
    display_name = item.display_name.strip() or path.name
    content_type = item.content_type.strip() or guess_content_type(path)

    try:
        file_bytes = path.read_bytes()
    except OSError as exc:
        return _error_outcome(path, f"Cannot read file: {exc}")
    if not file_bytes:
        return _error_outcome(path, "File is empty; nothing to upload.")

    url_result = get_upload_url(connection, token)
    if (
        not url_result.ok
        or not url_result.signed_url
        or not url_result.file_source
    ):
        return _error_outcome(
            path, url_result.error_message or "Failed to allocate upload URL."
        )

    bytes_result = upload_file_bytes(
        url_result.signed_url,
        file_bytes,
        content_type=content_type,
        timeout=UPLOAD_BYTES_TIMEOUT_SECONDS,
    )
    if not bytes_result.ok:
        return _error_outcome(
            path,
            bytes_result.error_message or "Failed to upload file bytes.",
            file_source=url_result.file_source,
        )

    meta = post_file_metadata(
        connection,
        token,
        file_source=url_result.file_source,
        file_id=url_result.file_id or "",
        display_name=display_name,
        description=item.description,
        legal_tag=legal_tag,
        acl_owners=acl_owners,
        acl_viewers=acl_viewers,
    )
    if not meta.ok:
        return _error_outcome(
            path,
            meta.error_message or "Failed to register file metadata.",
            bytes_uploaded=bytes_result.bytes_uploaded,
            file_source=url_result.file_source,
        )

    return FileUploadOutcome(
        source_path=path,
        filename=path.name,
        status="success",
        record_id=meta.record_id,
        record_version=meta.record_version,
        file_source=url_result.file_source,
        bytes_uploaded=bytes_result.bytes_uploaded,
        error=None,
    )
