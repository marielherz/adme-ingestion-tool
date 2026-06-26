"""Bulk ingestion queue — multi-manifest submit with retries and a breaker.

Operator flow (Judson's page wires these in order):

  1. Build a queue from uploaded files (``build_queue_from_files``) and/or
     pasted text (``parse_pasted_manifests``).
  2. Enforce the hard cap (``enforce_queue_size_limit``).
  3. Pre-validate every row (``validate_queue``) and surface the table.
  4. Stream submits (``submit_queue``) with per-row retry, an in-loop
     circuit breaker, and an abort hook.

Design notes
------------
*Submit goes through the existing :func:`app.services.ingestion.submit_manifest`*
so the workflow path, headers, error normalization, and telemetry stay
single-sourced.  ACL/legal stamping reuses
:func:`app.services.bulk_loader.inject_acl_and_legal` for the same reason.

*Retry-After header parsing is implemented* (:func:`_parse_retry_after`)
but is not yet wired through ``submit_manifest`` (which doesn't surface
response headers today).  Until that pipe lands, the runtime path uses
exponential backoff (1/2/4/8s, capped, with ±25% jitter) for every
retryable failure including HTTP 429.  Swapping in real Retry-After is a
one-line change once ``WorkflowRunResult`` carries headers.

*Aborted rows write nothing to run_history.* Per the 2026-05-22 final
lock §4, abort = "operator changed their mind", not a recoverable
failure.  Successful, rejected, breaker-skipped, and exhausted-error
rows DO write history.
"""

from __future__ import annotations

import email.utils
import logging
import random
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import requests

from app.models.connection import ADMEConnection
from app.models.osdu import (
    CircuitBreakerState,
    CircuitBreakerTripped,
    QueueItem,
    QueueSubmitResult,
    QueueValidationResult,
    RetryPolicy,
    WorkflowRunResult,
    WorkflowStatus,
)
from app.services.bulk_loader import inject_acl_and_legal
from app.services.ingestion import submit_manifest, validate_manifest_json
from app.services.run_history import (
    RUN_HISTORY_WRITE_ERRORS,
    record_workflow_finish,
    record_workflow_submit,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SUBMIT_SOURCE",
    "MAX_QUEUE_SIZE",
    "DEFAULT_RETRY_MAX_ATTEMPTS",
    "DEFAULT_BACKOFF_INITIAL_SECONDS",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "DEFAULT_BACKOFF_JITTER_RATIO",
    "DEFAULT_CIRCUIT_BREAKER_THRESHOLD",
    "DEFAULT_INTER_SUBMIT_DELAY_SECONDS",
    "UploadedFileLike",
    "parse_pasted_manifests",
    "build_queue_from_files",
    "validate_queue",
    "enforce_queue_size_limit",
    "submit_queue",
]

SUBMIT_SOURCE = "bulk_load"
MAX_QUEUE_SIZE = 500
DEFAULT_RETRY_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_INITIAL_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 8.0
DEFAULT_BACKOFF_JITTER_RATIO = 0.25
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 5
DEFAULT_INTER_SUBMIT_DELAY_SECONDS = 0.0

_MANIFEST_SECTIONS: tuple[str, ...] = ("ReferenceData", "MasterData", "Data")
_RETRYABLE_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.Timeout,
    requests.ConnectionError,
)


class UploadedFileLike(Protocol):
    """Minimal protocol matching ``streamlit.runtime.uploaded_file_manager.UploadedFile``."""

    name: str

    def getvalue(self) -> bytes:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Queue building
# ---------------------------------------------------------------------------


def parse_pasted_manifests(text: str) -> list[QueueItem]:
    """Split a textarea blob into one :class:`QueueItem` per manifest.

    Manifests are separated by lines containing only ``---`` (after
    strip).  Each chunk becomes a :class:`QueueItem` with
    ``source="pasted"`` and label ``"pasted-1"``..``"pasted-N"``.

    Raises ``ValueError`` when the input is empty or every chunk is
    whitespace-only.
    """
    if text is None or not text.strip():
        raise ValueError("No manifest text provided.")

    chunks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "---":
            chunks.append("\n".join(current))
            current = []
        else:
            current.append(line)
    chunks.append("\n".join(current))

    items: list[QueueItem] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        items.append(
            QueueItem(
                label=f"pasted-{len(items) + 1}",
                raw_text=chunk,
                source="pasted",
            )
        )

    if not items:
        raise ValueError("No manifest text provided.")
    return items


def build_queue_from_files(
    files: Sequence[UploadedFileLike],
) -> list[QueueItem]:
    """Decode each Streamlit ``UploadedFile`` into a :class:`QueueItem`.

    Bytes are decoded as UTF-8 with ``errors="replace"`` so an embedded
    invalid byte does not abort the whole batch — pre-validation will
    flag the row as not-valid-JSON.  Empty file list returns ``[]``.
    """
    items: list[QueueItem] = []
    for upload in files:
        raw_bytes = upload.getvalue()
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        items.append(
            QueueItem(
                label=upload.name,
                raw_text=raw_text,
                source="uploaded",
            )
        )
    return items


def enforce_queue_size_limit(
    items: Sequence[QueueItem],
    *,
    max_size: int = MAX_QUEUE_SIZE,
) -> None:
    """Raise ``ValueError`` when ``items`` exceeds the page hard cap.

    Caller (Judson's page) handles the message string; we just guard
    the boundary so a misbehaving page can't queue 10k rows.
    """
    if len(items) > max_size:
        raise ValueError(
            f"Queue size {len(items)} exceeds maximum of {max_size} rows."
        )


# ---------------------------------------------------------------------------
# Pre-submit validation
# ---------------------------------------------------------------------------


def _inner_manifest(parsed: dict | None) -> dict | None:
    """Return ``parsed["executionContext"]["manifest"]`` or ``None``."""
    if not isinstance(parsed, dict):
        return None
    ctx = parsed.get("executionContext")
    if not isinstance(ctx, dict):
        return None
    manifest = ctx.get("manifest")
    if isinstance(manifest, dict):
        return manifest
    return None


def _harvest_kinds_and_count(
    parsed: dict | None,
) -> tuple[tuple[str, ...], int]:
    """Return ``(distinct_kinds_in_first_seen_order, record_count)``."""
    manifest = _inner_manifest(parsed)
    if manifest is None:
        return (), 0
    seen: list[str] = []
    seen_set: set[str] = set()
    count = 0
    for section_key in _MANIFEST_SECTIONS:
        section = manifest.get(section_key)
        records: list[Any] = []
        if isinstance(section, list):
            records = section
        elif isinstance(section, dict):
            # Data section may be a dict with Datasets/WorkProductComponents/WorkProduct.
            for sub_key in ("Datasets", "WorkProductComponents"):
                sub = section.get(sub_key)
                if isinstance(sub, list):
                    records.extend(sub)
            wp = section.get("WorkProduct")
            if isinstance(wp, dict):
                records.append(wp)
        for record in records:
            if not isinstance(record, dict):
                continue
            count += 1
            kind = record.get("kind")
            if isinstance(kind, str) and kind and kind not in seen_set:
                seen.append(kind)
                seen_set.add(kind)
    return tuple(seen), count


def validate_queue(
    items: Sequence[QueueItem],
) -> list[QueueValidationResult]:
    """Pre-validate every queued manifest.  Never raises.

    Each item is parsed via :func:`app.services.ingestion.validate_manifest_json`;
    on success, kinds and record counts are harvested.
    """
    results: list[QueueValidationResult] = []
    for item in items:
        ok, error_message, parsed = validate_manifest_json(item.raw_text)
        if ok and isinstance(parsed, dict):
            kinds, count = _harvest_kinds_and_count(parsed)
            results.append(
                QueueValidationResult(
                    label=item.label,
                    ok=True,
                    kinds=kinds,
                    record_count=count,
                    parsed_manifest=parsed,
                    error_message=None,
                )
            )
        else:
            results.append(
                QueueValidationResult(
                    label=item.label,
                    ok=False,
                    kinds=(),
                    record_count=0,
                    parsed_manifest=None,
                    error_message=error_message or "Manifest failed validation.",
                )
            )
    return results


# ---------------------------------------------------------------------------
# Retry / backoff helpers
# ---------------------------------------------------------------------------


def _parse_retry_after(
    header_value: str | None,
    *,
    max_seconds: float = 60.0,
    now: datetime | None = None,
) -> float | None:
    """Parse an HTTP ``Retry-After`` header value to seconds, capped.

    Accepts either an integer/float delta-seconds or an HTTP-date string
    (RFC 7231).  Returns ``None`` for unparseable input.  Negative
    values clamp to ``0.0``.  Values above ``max_seconds`` clamp to
    ``max_seconds``.
    """
    if header_value is None:
        return None
    raw = header_value.strip()
    if not raw:
        return None
    # Try delta-seconds first.
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = None
    if seconds is None:
        # Try HTTP-date.
        try:
            target = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if target is None:
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        anchor = now or datetime.now(UTC)
        seconds = (target - anchor).total_seconds()
    if seconds < 0:
        seconds = 0.0
    if seconds > max_seconds:
        seconds = max_seconds
    return float(seconds)


def _compute_backoff_seconds(
    attempt: int,
    policy: RetryPolicy,
    *,
    rng: Callable[[float, float], float] = random.uniform,
) -> float:
    """Return seconds to sleep before the next attempt.

    ``attempt`` is the 1-based number of the *just-failed* attempt; the
    returned delay is what to sleep before attempt ``attempt + 1``.
    Schedule: initial, 2x, 4x, ... capped at ``backoff_cap_seconds``,
    multiplied by ``1 ± jitter_ratio``.
    """
    base = policy.backoff_initial_seconds * (2 ** max(0, attempt - 1))
    if base > policy.backoff_cap_seconds:
        base = policy.backoff_cap_seconds
    jitter = policy.backoff_jitter_ratio
    if jitter <= 0:
        return base
    return base * rng(1.0 - jitter, 1.0 + jitter)


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------


def _format_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_section_kind(parsed: dict | None) -> str | None:
    manifest = _inner_manifest(parsed)
    if manifest is None:
        return None
    for key in _MANIFEST_SECTIONS:
        section = manifest.get(key)
        records: list[Any] = []
        if isinstance(section, list):
            records = section
        elif isinstance(section, dict):
            for sub_key in ("Datasets", "WorkProductComponents"):
                sub = section.get(sub_key)
                if isinstance(sub, list):
                    records.extend(sub)
            wp = section.get("WorkProduct")
            if isinstance(wp, dict):
                records.append(wp)
        for record in records:
            if isinstance(record, dict):
                kind = record.get("kind")
                if isinstance(kind, str) and kind:
                    return kind
    return None


def _first_nonempty_section(parsed: dict | None) -> str:
    """Pick the section name to pass to :func:`inject_acl_and_legal`."""
    manifest = _inner_manifest(parsed)
    if manifest is None:
        return "ReferenceData"
    for key in _MANIFEST_SECTIONS:
        section = manifest.get(key)
        if isinstance(section, list) and section:
            return key
        if isinstance(section, dict) and section:
            return key
    return "ReferenceData"


def _safe_record_submit(**kwargs: Any) -> None:
    try:
        record_workflow_submit(**kwargs)
    except RUN_HISTORY_WRITE_ERRORS as exc:
        logger.warning("run_history submit-write failed: %s", exc)


def _safe_record_finish(**kwargs: Any) -> None:
    try:
        record_workflow_finish(**kwargs)
    except RUN_HISTORY_WRITE_ERRORS as exc:
        logger.warning("run_history finish-write failed: %s", exc)


def _record_success(
    *,
    run_id: str,
    submitted_at: datetime,
    kind: str | None,
    correlation_id: str | None,
    data_partition_id: str,
) -> None:
    _safe_record_submit(
        run_id=run_id,
        submitted_at=_format_utc_iso(submitted_at),
        kind=kind,
        correlation_id=correlation_id,
        submit_source=SUBMIT_SOURCE,
        data_partition_id=data_partition_id,
    )


def _record_failure(
    *,
    synthetic_prefix: str,
    submitted_at: datetime,
    kind: str | None,
    correlation_id: str | None,
    data_partition_id: str,
    error_message: str,
    latency_ms: int,
) -> str:
    run_id = f"{synthetic_prefix}:{uuid.uuid4()}"
    iso = _format_utc_iso(submitted_at)
    _safe_record_submit(
        run_id=run_id,
        submitted_at=iso,
        kind=kind,
        correlation_id=correlation_id,
        submit_source=SUBMIT_SOURCE,
        data_partition_id=data_partition_id,
    )
    _safe_record_finish(
        run_id=run_id,
        finished_at=iso,
        status=WorkflowStatus.FAILED,
        latency_ms=latency_ms,
        error_message=error_message,
    )
    return run_id


# ---------------------------------------------------------------------------
# Per-row submit with retry
# ---------------------------------------------------------------------------


def _submit_with_retry(
    *,
    payload: dict[str, Any],
    connection: ADMEConnection,
    token: str,
    policy: RetryPolicy,
    sleeper: Callable[[float], None],
    abort_check: Callable[[], bool] | None,
) -> tuple[WorkflowRunResult | None, int, str | None, BaseException | None]:
    """Drive :func:`submit_manifest` with retries.

    Returns ``(result, attempts, terminal_kind, transport_exc)``:
      * ``result`` — the final :class:`WorkflowRunResult` (or ``None`` if
        every attempt raised a transport exception).
      * ``attempts`` — number of HTTP attempts actually made.
      * ``terminal_kind`` — one of ``"success" | "non_retryable" |
        "throttled" | "server_error" | "transport" | "aborted"``.
      * ``transport_exc`` — last transport exception when terminal_kind
        is ``"transport"`` (else ``None``).
    """
    attempts = 0
    last_result: WorkflowRunResult | None = None
    last_exc: BaseException | None = None

    for attempt in range(1, policy.max_attempts + 1):
        if abort_check is not None and abort_check():
            return last_result, attempts, "aborted", last_exc

        attempts = attempt
        try:
            last_result = submit_manifest(connection, token, payload)
            last_exc = None
        except _RETRYABLE_TRANSPORT_EXCEPTIONS as exc:
            last_exc = exc
            last_result = None
            if attempt >= policy.max_attempts:
                return None, attempts, "transport", exc
            _sleep_with_abort(
                _compute_backoff_seconds(attempt, policy),
                sleeper=sleeper,
                abort_check=abort_check,
            )
            if abort_check is not None and abort_check():
                return None, attempts, "aborted", exc
            continue

        if last_result.ok and last_result.run_id:
            return last_result, attempts, "success", None

        http_status = last_result.http_status
        if http_status is None:
            # Transport-like result without exception (submit_manifest
            # normalized it).  Treat as retryable.
            kind = "transport"
        elif http_status == 429:
            kind = "throttled"
        elif 500 <= http_status < 600:
            kind = "server_error"
        else:
            return last_result, attempts, "non_retryable", None

        if attempt >= policy.max_attempts:
            return last_result, attempts, kind, None

        _sleep_with_abort(
            _compute_backoff_seconds(attempt, policy),
            sleeper=sleeper,
            abort_check=abort_check,
        )
        if abort_check is not None and abort_check():
            return last_result, attempts, "aborted", None

    return last_result, attempts, "non_retryable", last_exc


def _sleep_with_abort(
    seconds: float,
    *,
    sleeper: Callable[[float], None],
    abort_check: Callable[[], bool] | None,
) -> None:
    """Sleep ``seconds``; when ``abort_check`` is provided, slice into 0.25s polls.

    Tests inject a deterministic ``sleeper`` (no abort hook), so the
    short-poll branch is exercised only in production-style paths.
    """
    if seconds <= 0:
        return
    if abort_check is None:
        sleeper(seconds)
        return
    remaining = seconds
    slice_size = 0.25
    while remaining > 0:
        if abort_check():
            return
        chunk = min(slice_size, remaining)
        sleeper(chunk)
        remaining -= chunk


# ---------------------------------------------------------------------------
# Queue driver
# ---------------------------------------------------------------------------


def submit_queue(
    items: Sequence[QueueItem],
    validations: Sequence[QueueValidationResult],
    *,
    acl_owners: Sequence[str],
    acl_viewers: Sequence[str],
    legal_tag: str,
    data_partition_id: str,
    connection: ADMEConnection,
    token: str,
    skip_invalid: bool = True,
    inter_submit_delay_seconds: float = DEFAULT_INTER_SUBMIT_DELAY_SECONDS,
    retry_policy: RetryPolicy | None = None,
    circuit_breaker_threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    abort_check: Callable[[], bool] | None = None,
    progress_callback: Callable[..., None] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> Iterator[QueueSubmitResult]:
    """Stream :class:`QueueSubmitResult` rows for every item in ``items``.

    Validation, ACL/legal stamping, retry, and the in-loop circuit
    breaker are all handled here.  See module docstring for the
    operator flow.

    ``progress_callback`` is invoked with positional ``(row_index,
    state)`` and keyword ``result``/``attempt``/``trip`` for live UI
    updates.  States: ``"submitting"``, ``"retrying"``, ``"success"``,
    ``"error"``, ``"rejected"``, ``"skipped"``, ``"breaker_tripped"``.
    """
    if len(items) != len(validations):
        raise ValueError(
            "items and validations must have the same length "
            f"(got {len(items)} and {len(validations)})."
        )
    policy = retry_policy or RetryPolicy()
    sleep_fn: Callable[[float], None] = sleeper or time.sleep
    breaker = CircuitBreakerState(threshold=circuit_breaker_threshold)
    failing_labels: list[str] = []
    breaker_emitted = False

    def _emit(
        row_index: int,
        state: str,
        *,
        result: QueueSubmitResult | None = None,
        attempt: int | None = None,
        trip: CircuitBreakerTripped | None = None,
    ) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                row_index, state, result=result, attempt=attempt, trip=trip
            )
        except Exception:  # noqa: BLE001 - UI callback must never break the loop
            logger.exception("progress_callback raised; continuing")

    for row_index, (item, validation) in enumerate(zip(items, validations)):
        # 1. Aborted by operator?
        if abort_check is not None and abort_check():
            now = datetime.now(UTC)
            result = QueueSubmitResult(
                label=item.label,
                status="skipped",
                run_id=None,
                correlation_id=None,
                http_status=None,
                latency_ms=None,
                error_message="aborted by operator",
                submitted_at=now,
                attempts=0,
                raw_text=item.raw_text,
            )
            _emit(row_index, "skipped", result=result)
            yield result
            continue

        # 2. Pre-validation reject.
        if not validation.ok and skip_invalid:
            now = datetime.now(UTC)
            reason = validation.error_message or "invalid manifest"
            error_message = f"rejected: {reason}"
            run_id = _record_failure(
                synthetic_prefix="bulk-load:rejected",
                submitted_at=now,
                kind=None,
                correlation_id=None,
                data_partition_id=data_partition_id,
                error_message=error_message,
                latency_ms=0,
            )
            result = QueueSubmitResult(
                label=item.label,
                status="rejected",
                run_id=run_id,
                correlation_id=None,
                http_status=None,
                latency_ms=0,
                error_message=error_message,
                submitted_at=now,
                attempts=0,
                raw_text=item.raw_text,
            )
            _emit(row_index, "rejected", result=result)
            yield result
            # Reject neither bumps nor resets the breaker counter.
            continue

        # 3. Breaker is open — skip remaining rows.
        if breaker.is_tripped:
            now = datetime.now(UTC)
            run_id = _record_failure(
                synthetic_prefix="bulk-load:skipped",
                submitted_at=now,
                kind=_first_section_kind(validation.parsed_manifest),
                correlation_id=None,
                data_partition_id=data_partition_id,
                error_message="skipped: circuit breaker",
                latency_ms=0,
            )
            result = QueueSubmitResult(
                label=item.label,
                status="skipped",
                run_id=run_id,
                correlation_id=None,
                http_status=None,
                latency_ms=0,
                error_message="skipped: circuit breaker",
                submitted_at=now,
                attempts=0,
                raw_text=item.raw_text,
            )
            _emit(row_index, "skipped", result=result)
            yield result
            continue

        # 4. Submit (with retry).
        parsed = validation.parsed_manifest
        inner = _inner_manifest(parsed)
        if not isinstance(inner, dict):
            # ``skip_invalid=False`` and validation said not-ok: synthesize a
            # parsed body from the raw text via a final json attempt.  If that
            # also fails, surface as an immediate error row.
            now = datetime.now(UTC)
            error_message = (
                f"error: {validation.error_message or 'invalid manifest'}"
            )
            run_id = _record_failure(
                synthetic_prefix="bulk-load:error",
                submitted_at=now,
                kind=None,
                correlation_id=None,
                data_partition_id=data_partition_id,
                error_message=error_message,
                latency_ms=0,
            )
            result = QueueSubmitResult(
                label=item.label,
                status="error",
                run_id=run_id,
                correlation_id=None,
                http_status=None,
                latency_ms=0,
                error_message=error_message,
                submitted_at=now,
                attempts=0,
                raw_text=item.raw_text,
            )
            _emit(row_index, "error", result=result)
            failing_labels.append(item.label)
            breaker, trip = _bump_breaker(
                breaker,
                failing_labels=failing_labels,
                label=item.label,
                http_status=None,
                error_message=error_message,
                remaining_count=len(items) - row_index - 1,
            )
            if trip is not None and not breaker_emitted:
                _emit(row_index, "breaker_tripped", trip=trip)
                breaker_emitted = True
            yield result
            continue

        section = _first_nonempty_section(parsed)
        try:
            shaped = inject_acl_and_legal(
                inner,
                section=section,
                acl_owners=acl_owners,
                acl_viewers=acl_viewers,
                legal_tag=legal_tag,
            )
        except Exception as exc:  # noqa: BLE001 - safety net
            now = datetime.now(UTC)
            error_message = f"error: ACL/legal injection failed — {exc}"
            run_id = _record_failure(
                synthetic_prefix="bulk-load:error",
                submitted_at=now,
                kind=_first_section_kind(parsed),
                correlation_id=None,
                data_partition_id=data_partition_id,
                error_message=error_message,
                latency_ms=0,
            )
            result = QueueSubmitResult(
                label=item.label,
                status="error",
                run_id=run_id,
                correlation_id=None,
                http_status=None,
                latency_ms=0,
                error_message=error_message,
                submitted_at=now,
                attempts=0,
                raw_text=item.raw_text,
            )
            _emit(row_index, "error", result=result)
            yield result
            continue

        payload = {
            "executionContext": {
                "Payload": {
                    "AppKey": "adme-ingestion-tool",
                    "data-partition-id": data_partition_id,
                },
                "manifest": shaped,
            }
        }

        _emit(row_index, "submitting", attempt=1)

        def _wrapped_sleeper(seconds: float, _idx: int = row_index) -> None:
            # Notify the UI before each retry sleep.  ``_idx`` captures the
            # current row defensively.
            _emit(_idx, "retrying", attempt=None)
            sleep_fn(seconds)

        now = datetime.now(UTC)
        retry_result, attempts, terminal_kind, transport_exc = (
            _submit_with_retry(
                payload=payload,
                connection=connection,
                token=token,
                policy=policy,
                sleeper=_wrapped_sleeper,
                abort_check=abort_check,
            )
        )

        if terminal_kind == "success" and retry_result is not None:
            assert retry_result.run_id is not None
            _record_success(
                run_id=retry_result.run_id,
                submitted_at=now,
                kind=_first_section_kind(parsed),
                correlation_id=retry_result.correlation_id,
                data_partition_id=data_partition_id,
            )
            result = QueueSubmitResult(
                label=item.label,
                status="success",
                run_id=retry_result.run_id,
                correlation_id=retry_result.correlation_id,
                http_status=retry_result.http_status,
                latency_ms=int(retry_result.latency_ms or 0),
                error_message=None,
                submitted_at=now,
                attempts=attempts,
                raw_text=item.raw_text,
            )
            _emit(row_index, "success", result=result)
            # Success resets the breaker counter (decisions.md 2026-05-22).
            breaker = CircuitBreakerState(
                consecutive_failures=0,
                is_tripped=breaker.is_tripped,
                threshold=breaker.threshold,
                tripped_at=breaker.tripped_at,
                tripping_label=breaker.tripping_label,
            )
            failing_labels = []
            yield result
            if inter_submit_delay_seconds > 0:
                _sleep_with_abort(
                    inter_submit_delay_seconds,
                    sleeper=sleep_fn,
                    abort_check=abort_check,
                )
            continue

        if terminal_kind == "aborted":
            now2 = datetime.now(UTC)
            result = QueueSubmitResult(
                label=item.label,
                status="skipped",
                run_id=None,
                correlation_id=(
                    retry_result.correlation_id if retry_result else None
                ),
                http_status=(
                    retry_result.http_status if retry_result else None
                ),
                latency_ms=None,
                error_message="aborted by operator",
                submitted_at=now2,
                attempts=attempts,
                raw_text=item.raw_text,
            )
            _emit(row_index, "skipped", result=result)
            yield result
            continue

        # Terminal failure paths.
        http_status = retry_result.http_status if retry_result else None
        correlation_id = retry_result.correlation_id if retry_result else None
        latency_ms = int((retry_result.latency_ms or 0) if retry_result else 0)
        if terminal_kind == "throttled":
            error_message = (
                f"throttled: HTTP 429 after {attempts} attempts"
            )
        elif terminal_kind == "server_error":
            error_message = (
                f"error: HTTP {http_status} after {attempts} attempts"
            )
        elif terminal_kind == "transport":
            exc_name = (
                type(transport_exc).__name__ if transport_exc else "transport"
            )
            error_message = (
                f"transport: {exc_name} after {attempts} attempts"
            )
        else:  # non_retryable
            server_msg = (
                retry_result.error_message
                if retry_result and retry_result.error_message
                else "non-2xx response"
            )
            error_message = f"non-retryable: HTTP {http_status} — {server_msg}"

        run_id = _record_failure(
            synthetic_prefix="bulk-load:error",
            submitted_at=now,
            kind=_first_section_kind(parsed),
            correlation_id=correlation_id,
            data_partition_id=data_partition_id,
            error_message=error_message,
            latency_ms=latency_ms,
        )
        result = QueueSubmitResult(
            label=item.label,
            status="error",
            run_id=run_id,
            correlation_id=correlation_id,
            http_status=http_status,
            latency_ms=latency_ms,
            error_message=error_message,
            submitted_at=now,
            attempts=attempts,
            raw_text=item.raw_text,
        )
        _emit(row_index, "error", result=result)
        failing_labels.append(item.label)
        breaker, trip = _bump_breaker(
            breaker,
            failing_labels=failing_labels,
            label=item.label,
            http_status=http_status,
            error_message=error_message,
            remaining_count=len(items) - row_index - 1,
        )
        if trip is not None and not breaker_emitted:
            _emit(row_index, "breaker_tripped", trip=trip)
            breaker_emitted = True
        yield result
        if inter_submit_delay_seconds > 0:
            _sleep_with_abort(
                inter_submit_delay_seconds,
                sleeper=sleep_fn,
                abort_check=abort_check,
            )


def _bump_breaker(
    state: CircuitBreakerState,
    *,
    failing_labels: Sequence[str],
    label: str,
    http_status: int | None,
    error_message: str | None,
    remaining_count: int,
) -> tuple[CircuitBreakerState, CircuitBreakerTripped | None]:
    """Increment the consecutive-failures counter; emit trip when crossed."""
    new_count = state.consecutive_failures + 1
    if new_count >= state.threshold and not state.is_tripped:
        now = datetime.now(UTC)
        tripped = CircuitBreakerState(
            consecutive_failures=new_count,
            is_tripped=True,
            threshold=state.threshold,
            tripped_at=now,
            tripping_label=label,
        )
        trip = CircuitBreakerTripped(
            tripped_at=now,
            threshold=state.threshold,
            failing_labels=tuple(failing_labels),
            last_http_status=http_status,
            last_error_message=error_message,
            remaining_count=remaining_count,
        )
        return tripped, trip
    return (
        CircuitBreakerState(
            consecutive_failures=new_count,
            is_tripped=state.is_tripped,
            threshold=state.threshold,
            tripped_at=state.tripped_at,
            tripping_label=state.tripping_label,
        ),
        None,
    )
