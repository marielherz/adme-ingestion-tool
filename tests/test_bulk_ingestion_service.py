"""Tests for ``app.services.bulk_ingestion``.

Service-layer tests only.  We monkeypatch ``submit_manifest`` and the
``record_workflow_*`` writers on the ``bulk_ingestion`` module (same
idiom as :mod:`tests.test_bulk_loader_service`) and inject a
deterministic sleeper so retry timing is observable.
"""

from __future__ import annotations

import io
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
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
from app.services import bulk_ingestion


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeUpload:
    """Minimal Streamlit ``UploadedFile`` stand-in."""

    def __init__(self, name: str, content: bytes) -> None:
        self.name = name
        self._content = content

    def getvalue(self) -> bytes:
        return self._content


def _connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
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
        latency_ms=12.0,
        correlation_id="corr-1",
        error_message=None,
    )


def _fail_result(http_status: int, error: str = "boom") -> WorkflowRunResult:
    return WorkflowRunResult(
        workflow_id=None,
        run_id=None,
        status=WorkflowStatus.UNKNOWN,
        raw_status="",
        message=None,
        ok=False,
        http_status=http_status,
        latency_ms=8.0,
        correlation_id="corr-fail",
        error_message=error,
    )


def _good_manifest_text(record_id: str = "id-1", kind: str = "osdu:wks:reference-data--Foo:1.0.0") -> str:
    body = {
        "executionContext": {
            "Payload": {"AppKey": "x", "data-partition-id": "opendes"},
            "manifest": {
                "kind": "Manifest",
                "ReferenceData": [
                    {
                        "id": record_id,
                        "kind": kind,
                        "acl": {"owners": [], "viewers": []},
                        "legal": {"legaltags": [], "otherRelevantDataCountries": []},
                        "data": {"Name": "foo"},
                    }
                ],
            },
        }
    }
    return json.dumps(body)


def _patch_history(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    submits: list[dict[str, Any]] = []
    finishes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        bulk_ingestion, "record_workflow_submit",
        lambda **kw: submits.append(kw),
    )
    monkeypatch.setattr(
        bulk_ingestion, "record_workflow_finish",
        lambda **kw: finishes.append(kw),
    )
    return submits, finishes


# ---------------------------------------------------------------------------
# Dataclass / model surface
# ---------------------------------------------------------------------------


def test_queue_item_is_frozen() -> None:
    item = QueueItem(label="x", raw_text="{}", source="pasted")
    with pytest.raises(FrozenInstanceError):
        item.label = "y"  # type: ignore[misc]


def test_retry_policy_defaults_match_module_constants() -> None:
    policy = RetryPolicy()
    assert policy.max_attempts == bulk_ingestion.DEFAULT_RETRY_MAX_ATTEMPTS
    assert policy.backoff_initial_seconds == bulk_ingestion.DEFAULT_BACKOFF_INITIAL_SECONDS
    assert policy.backoff_cap_seconds == bulk_ingestion.DEFAULT_BACKOFF_CAP_SECONDS
    assert policy.backoff_jitter_ratio == bulk_ingestion.DEFAULT_BACKOFF_JITTER_RATIO
    assert policy.retry_after_max_seconds == 60.0


def test_circuit_breaker_state_defaults() -> None:
    state = CircuitBreakerState()
    assert state.consecutive_failures == 0
    assert state.is_tripped is False
    assert state.threshold == 5
    assert state.tripped_at is None
    assert state.tripping_label is None


def test_queue_submit_result_carries_attempts_and_raw_text() -> None:
    result = QueueSubmitResult(
        label="x",
        status="success",
        run_id="r",
        correlation_id=None,
        http_status=200,
        latency_ms=5,
        error_message=None,
        submitted_at=datetime.now(UTC),
        attempts=2,
        raw_text="{}",
    )
    assert result.attempts == 2
    assert result.raw_text == "{}"


def test_circuit_breaker_tripped_carries_remaining_count() -> None:
    trip = CircuitBreakerTripped(
        tripped_at=datetime.now(UTC),
        threshold=5,
        failing_labels=("a", "b"),
        last_http_status=500,
        last_error_message="boom",
        remaining_count=12,
    )
    assert trip.remaining_count == 12
    assert trip.failing_labels == ("a", "b")


# ---------------------------------------------------------------------------
# parse_pasted_manifests
# ---------------------------------------------------------------------------


def test_parse_pasted_manifests_splits_on_triple_dash() -> None:
    text = "{\"a\": 1}\n---\n{\"b\": 2}\n---\n{\"c\": 3}"
    items = bulk_ingestion.parse_pasted_manifests(text)
    assert [i.label for i in items] == ["pasted-1", "pasted-2", "pasted-3"]
    assert all(i.source == "pasted" for i in items)
    assert items[0].raw_text.strip() == '{"a": 1}'


def test_parse_pasted_manifests_single_chunk() -> None:
    items = bulk_ingestion.parse_pasted_manifests('{"hello": "world"}')
    assert len(items) == 1
    assert items[0].label == "pasted-1"


def test_parse_pasted_manifests_ignores_empty_chunks() -> None:
    items = bulk_ingestion.parse_pasted_manifests("{\"a\": 1}\n---\n   \n---\n{\"b\": 2}")
    assert [i.label for i in items] == ["pasted-1", "pasted-2"]


def test_parse_pasted_manifests_empty_raises() -> None:
    with pytest.raises(ValueError, match="No manifest text"):
        bulk_ingestion.parse_pasted_manifests("")
    with pytest.raises(ValueError, match="No manifest text"):
        bulk_ingestion.parse_pasted_manifests("   \n  ")
    with pytest.raises(ValueError, match="No manifest text"):
        bulk_ingestion.parse_pasted_manifests("---\n---\n")


# ---------------------------------------------------------------------------
# build_queue_from_files
# ---------------------------------------------------------------------------


def test_build_queue_from_files_uses_filename_as_label() -> None:
    files = [
        _FakeUpload("a.json", b'{"a": 1}'),
        _FakeUpload("b.json", b'{"b": 2}'),
    ]
    items = bulk_ingestion.build_queue_from_files(files)
    assert [i.label for i in items] == ["a.json", "b.json"]
    assert all(i.source == "uploaded" for i in items)


def test_build_queue_from_files_handles_invalid_utf8() -> None:
    files = [_FakeUpload("bad.json", b"\xff\xfeoops")]
    items = bulk_ingestion.build_queue_from_files(files)
    # errors="replace" => parse will surface a JSON error downstream, not an exception here.
    assert len(items) == 1
    assert items[0].label == "bad.json"


def test_build_queue_from_files_empty_list() -> None:
    assert bulk_ingestion.build_queue_from_files([]) == []


# ---------------------------------------------------------------------------
# enforce_queue_size_limit
# ---------------------------------------------------------------------------


def test_enforce_queue_size_limit_under_cap_ok() -> None:
    items = [QueueItem(label=f"q-{i}", raw_text="{}", source="pasted") for i in range(3)]
    bulk_ingestion.enforce_queue_size_limit(items, max_size=10)


def test_enforce_queue_size_limit_at_cap_ok() -> None:
    items = [QueueItem(label=f"q-{i}", raw_text="{}", source="pasted") for i in range(5)]
    bulk_ingestion.enforce_queue_size_limit(items, max_size=5)


def test_enforce_queue_size_limit_over_cap_raises() -> None:
    items = [QueueItem(label=f"q-{i}", raw_text="{}", source="pasted") for i in range(6)]
    with pytest.raises(ValueError, match="exceeds maximum"):
        bulk_ingestion.enforce_queue_size_limit(items, max_size=5)


def test_enforce_queue_size_limit_default_cap_is_500() -> None:
    assert bulk_ingestion.MAX_QUEUE_SIZE == 500


# ---------------------------------------------------------------------------
# validate_queue
# ---------------------------------------------------------------------------


def test_validate_queue_marks_good_manifest_ok_and_harvests_kinds() -> None:
    items = [QueueItem(label="ok", raw_text=_good_manifest_text(), source="pasted")]
    results = bulk_ingestion.validate_queue(items)
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].record_count == 1
    assert results[0].kinds == ("osdu:wks:reference-data--Foo:1.0.0",)
    assert isinstance(results[0].parsed_manifest, dict)


def test_validate_queue_flags_bad_json() -> None:
    items = [QueueItem(label="bad", raw_text="{ not json", source="pasted")]
    results = bulk_ingestion.validate_queue(items)
    assert results[0].ok is False
    assert results[0].error_message is not None
    assert results[0].parsed_manifest is None


def test_validate_queue_never_raises_on_mixed_input() -> None:
    items = [
        QueueItem(label="ok", raw_text=_good_manifest_text(), source="pasted"),
        QueueItem(label="empty", raw_text="", source="pasted"),
        QueueItem(label="bad", raw_text="not json", source="pasted"),
    ]
    results = bulk_ingestion.validate_queue(items)
    assert [r.ok for r in results] == [True, False, False]


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------


def test_parse_retry_after_parses_integer_seconds() -> None:
    assert bulk_ingestion._parse_retry_after("5") == 5.0


def test_parse_retry_after_clamps_to_max_seconds() -> None:
    assert bulk_ingestion._parse_retry_after("999", max_seconds=60) == 60.0


def test_parse_retry_after_negative_clamps_to_zero() -> None:
    assert bulk_ingestion._parse_retry_after("-5") == 0.0


def test_parse_retry_after_returns_none_for_missing() -> None:
    assert bulk_ingestion._parse_retry_after(None) is None
    assert bulk_ingestion._parse_retry_after("") is None
    assert bulk_ingestion._parse_retry_after("   ") is None


def test_parse_retry_after_http_date() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    later = (now + timedelta(seconds=10)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    seconds = bulk_ingestion._parse_retry_after(later, now=now)
    assert seconds is not None
    assert 9.5 <= seconds <= 10.5


def test_parse_retry_after_garbage_returns_none() -> None:
    assert bulk_ingestion._parse_retry_after("not-a-date") is None


def test_compute_backoff_seconds_schedule_with_no_jitter() -> None:
    policy = RetryPolicy(
        max_attempts=4,
        backoff_initial_seconds=1.0,
        backoff_cap_seconds=8.0,
        backoff_jitter_ratio=0.0,
    )
    assert bulk_ingestion._compute_backoff_seconds(1, policy) == 1.0
    assert bulk_ingestion._compute_backoff_seconds(2, policy) == 2.0
    assert bulk_ingestion._compute_backoff_seconds(3, policy) == 4.0
    assert bulk_ingestion._compute_backoff_seconds(4, policy) == 8.0
    # Caps at backoff_cap_seconds.
    assert bulk_ingestion._compute_backoff_seconds(5, policy) == 8.0


def test_compute_backoff_seconds_applies_jitter() -> None:
    policy = RetryPolicy(backoff_initial_seconds=1.0, backoff_jitter_ratio=0.25)
    # rng=lambda lo, hi: lo gives the lower bound.
    low = bulk_ingestion._compute_backoff_seconds(1, policy, rng=lambda lo, _hi: lo)
    high = bulk_ingestion._compute_backoff_seconds(1, policy, rng=lambda _lo, hi: hi)
    assert low == pytest.approx(0.75)
    assert high == pytest.approx(1.25)


# ---------------------------------------------------------------------------
# submit_queue — happy path
# ---------------------------------------------------------------------------


def test_submit_queue_happy_path_one_row(monkeypatch: pytest.MonkeyPatch) -> None:
    submits, finishes = _patch_history(monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_submit(connection: ADMEConnection, token: str, payload: dict) -> WorkflowRunResult:
        calls.append(payload)
        return _ok_result(run_id="run-happy-1")

    monkeypatch.setattr(bulk_ingestion, "submit_manifest", fake_submit)

    items = [QueueItem(label="ok", raw_text=_good_manifest_text(), source="pasted")]
    validations = bulk_ingestion.validate_queue(items)

    results = list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["owner@example"],
            acl_viewers=["viewer@example"],
            legal_tag="example-legal",
            data_partition_id="opendes",
            connection=_connection(),
            token="tok",
            sleeper=lambda _s: None,
        )
    )
    assert len(results) == 1
    assert results[0].status == "success"
    assert results[0].run_id == "run-happy-1"
    assert results[0].attempts == 1
    assert results[0].raw_text == items[0].raw_text
    assert len(calls) == 1
    # ACL was injected.
    assert calls[0]["executionContext"]["manifest"]["ReferenceData"][0]["acl"]["owners"] == ["owner@example"]
    assert len(submits) == 1
    assert submits[0]["submit_source"] == "bulk_load"
    assert finishes == []  # success doesn't write finish


def test_submit_queue_skips_invalid_when_skip_invalid_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submits, finishes = _patch_history(monkeypatch)
    submit_called = False

    def fake_submit(*_a: Any, **_kw: Any) -> WorkflowRunResult:
        nonlocal submit_called
        submit_called = True
        return _ok_result()

    monkeypatch.setattr(bulk_ingestion, "submit_manifest", fake_submit)

    items = [QueueItem(label="bad", raw_text="not json", source="pasted")]
    validations = bulk_ingestion.validate_queue(items)
    results = list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["o"], acl_viewers=["v"],
            legal_tag="L", data_partition_id="opendes",
            connection=_connection(), token="t",
            skip_invalid=True,
            sleeper=lambda _s: None,
        )
    )
    assert submit_called is False
    assert results[0].status == "rejected"
    assert results[0].error_message.startswith("rejected:")
    assert results[0].attempts == 0
    assert results[0].run_id is not None
    assert results[0].run_id.startswith("bulk-load:rejected:")
    assert len(submits) == 1
    assert submits[0]["run_id"].startswith("bulk-load:rejected:")
    assert len(finishes) == 1
    assert finishes[0]["status"] == WorkflowStatus.FAILED


# ---------------------------------------------------------------------------
# submit_queue — retry behavior
# ---------------------------------------------------------------------------


def test_submit_queue_retries_on_500_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_history(monkeypatch)
    sleeps: list[float] = []
    attempts: list[int] = []

    responses = [
        _fail_result(500, "server boom"),
        _fail_result(500, "server boom"),
        _ok_result(run_id="run-eventually"),
    ]

    def fake_submit(*_a: Any, **_kw: Any) -> WorkflowRunResult:
        attempts.append(1)
        return responses[len(attempts) - 1]

    monkeypatch.setattr(bulk_ingestion, "submit_manifest", fake_submit)
    items = [QueueItem(label="ok", raw_text=_good_manifest_text(), source="pasted")]
    validations = bulk_ingestion.validate_queue(items)

    policy = RetryPolicy(max_attempts=3, backoff_jitter_ratio=0.0)
    results = list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["o"], acl_viewers=["v"],
            legal_tag="L", data_partition_id="opendes",
            connection=_connection(), token="t",
            retry_policy=policy,
            sleeper=sleeps.append,
        )
    )
    assert len(attempts) == 3
    assert results[0].status == "success"
    assert results[0].attempts == 3
    assert sleeps == [1.0, 2.0]


def test_submit_queue_exhausts_retries_on_persistent_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submits, finishes = _patch_history(monkeypatch)
    monkeypatch.setattr(
        bulk_ingestion, "submit_manifest",
        lambda *_a, **_kw: _fail_result(500, "still broken"),
    )
    items = [QueueItem(label="ok", raw_text=_good_manifest_text(), source="pasted")]
    validations = bulk_ingestion.validate_queue(items)
    policy = RetryPolicy(max_attempts=3, backoff_jitter_ratio=0.0)

    results = list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["o"], acl_viewers=["v"],
            legal_tag="L", data_partition_id="opendes",
            connection=_connection(), token="t",
            retry_policy=policy,
            sleeper=lambda _s: None,
        )
    )
    assert results[0].status == "error"
    assert results[0].attempts == 3
    assert "HTTP 500 after 3 attempts" in results[0].error_message
    assert results[0].run_id.startswith("bulk-load:error:")
    assert len(finishes) == 1
    assert finishes[0]["status"] == WorkflowStatus.FAILED


def test_submit_queue_non_retryable_400_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_history(monkeypatch)
    attempts = [0]

    def fake_submit(*_a: Any, **_kw: Any) -> WorkflowRunResult:
        attempts[0] += 1
        return _fail_result(400, "bad schema")

    monkeypatch.setattr(bulk_ingestion, "submit_manifest", fake_submit)
    items = [QueueItem(label="ok", raw_text=_good_manifest_text(), source="pasted")]
    validations = bulk_ingestion.validate_queue(items)
    results = list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["o"], acl_viewers=["v"],
            legal_tag="L", data_partition_id="opendes",
            connection=_connection(), token="t",
            retry_policy=RetryPolicy(max_attempts=3, backoff_jitter_ratio=0.0),
            sleeper=lambda _s: None,
        )
    )
    assert attempts[0] == 1  # no retry on 4xx
    assert results[0].status == "error"
    assert "non-retryable" in results[0].error_message
    assert "HTTP 400" in results[0].error_message


def test_submit_queue_transport_exception_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_history(monkeypatch)
    calls = [0]

    def fake_submit(*_a: Any, **_kw: Any) -> WorkflowRunResult:
        calls[0] += 1
        if calls[0] < 2:
            raise requests.ConnectionError("network down")
        return _ok_result(run_id="r-after-transport")

    monkeypatch.setattr(bulk_ingestion, "submit_manifest", fake_submit)
    items = [QueueItem(label="ok", raw_text=_good_manifest_text(), source="pasted")]
    validations = bulk_ingestion.validate_queue(items)
    results = list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["o"], acl_viewers=["v"],
            legal_tag="L", data_partition_id="opendes",
            connection=_connection(), token="t",
            retry_policy=RetryPolicy(max_attempts=3, backoff_jitter_ratio=0.0),
            sleeper=lambda _s: None,
        )
    )
    assert results[0].status == "success"
    assert results[0].attempts == 2


# ---------------------------------------------------------------------------
# submit_queue — circuit breaker
# ---------------------------------------------------------------------------


def test_submit_queue_trips_breaker_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submits, finishes = _patch_history(monkeypatch)
    monkeypatch.setattr(
        bulk_ingestion, "submit_manifest",
        lambda *_a, **_kw: _fail_result(500, "boom"),
    )
    items = [
        QueueItem(label=f"row-{i}", raw_text=_good_manifest_text(record_id=f"id-{i}"), source="pasted")
        for i in range(8)
    ]
    validations = bulk_ingestion.validate_queue(items)
    trips: list[CircuitBreakerTripped] = []

    def progress(_idx: int, state: str, **kw: Any) -> None:
        if state == "breaker_tripped" and kw.get("trip") is not None:
            trips.append(kw["trip"])

    results = list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["o"], acl_viewers=["v"],
            legal_tag="L", data_partition_id="opendes",
            connection=_connection(), token="t",
            retry_policy=RetryPolicy(max_attempts=1, backoff_jitter_ratio=0.0),
            circuit_breaker_threshold=3,
            sleeper=lambda _s: None,
            progress_callback=progress,
        )
    )
    # First 3 rows fail (error), breaker trips on row 3, remaining 5 are skipped.
    statuses = [r.status for r in results]
    assert statuses[:3] == ["error", "error", "error"]
    assert statuses[3:] == ["skipped", "skipped", "skipped", "skipped", "skipped"]
    assert len(trips) == 1
    assert trips[0].threshold == 3
    assert trips[0].remaining_count == 5
    # Skipped rows record history rows too.
    skipped_writes = [s for s in submits if s["run_id"].startswith("bulk-load:skipped:")]
    assert len(skipped_writes) == 5


def test_submit_queue_success_resets_breaker_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_history(monkeypatch)
    responses: list[WorkflowRunResult] = [
        _fail_result(500), _fail_result(500),
        _ok_result(run_id="recover"),
        _fail_result(500), _fail_result(500),
    ]
    idx = [0]

    def fake_submit(*_a: Any, **_kw: Any) -> WorkflowRunResult:
        result = responses[idx[0]]
        idx[0] += 1
        return result

    monkeypatch.setattr(bulk_ingestion, "submit_manifest", fake_submit)
    items = [
        QueueItem(label=f"row-{i}", raw_text=_good_manifest_text(record_id=f"id-{i}"), source="pasted")
        for i in range(5)
    ]
    validations = bulk_ingestion.validate_queue(items)
    results = list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["o"], acl_viewers=["v"],
            legal_tag="L", data_partition_id="opendes",
            connection=_connection(), token="t",
            retry_policy=RetryPolicy(max_attempts=1, backoff_jitter_ratio=0.0),
            circuit_breaker_threshold=3,
            sleeper=lambda _s: None,
        )
    )
    # 2 errors, 1 success (resets), 2 errors — total error<threshold => no trip.
    assert [r.status for r in results] == ["error", "error", "success", "error", "error"]


# ---------------------------------------------------------------------------
# submit_queue — abort
# ---------------------------------------------------------------------------


def test_submit_queue_abort_skips_remaining_rows_without_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submits, finishes = _patch_history(monkeypatch)
    calls = [0]

    def fake_submit(*_a: Any, **_kw: Any) -> WorkflowRunResult:
        calls[0] += 1
        return _ok_result(run_id=f"run-{calls[0]}")

    monkeypatch.setattr(bulk_ingestion, "submit_manifest", fake_submit)
    items = [
        QueueItem(label=f"row-{i}", raw_text=_good_manifest_text(record_id=f"id-{i}"), source="pasted")
        for i in range(4)
    ]
    validations = bulk_ingestion.validate_queue(items)

    # Abort after the first successful submit.
    abort_state = [False]

    def abort_check() -> bool:
        return abort_state[0]

    def progress(idx: int, state: str, **_kw: Any) -> None:
        if idx == 0 and state == "success":
            abort_state[0] = True

    results = list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["o"], acl_viewers=["v"],
            legal_tag="L", data_partition_id="opendes",
            connection=_connection(), token="t",
            sleeper=lambda _s: None,
            abort_check=abort_check,
            progress_callback=progress,
        )
    )
    assert results[0].status == "success"
    assert all(r.status == "skipped" for r in results[1:])
    assert all(r.error_message == "aborted by operator" for r in results[1:])
    # Aborted rows write NOTHING to history (per 2026-05-22 final lock §4).
    skipped_writes = [s for s in submits if s["run_id"].startswith("bulk-load:skipped:")]
    assert skipped_writes == []
    abort_finishes = [f for f in finishes if "aborted" in (f.get("error_message") or "")]
    assert abort_finishes == []


# ---------------------------------------------------------------------------
# submit_queue — progress_callback shape
# ---------------------------------------------------------------------------


def test_submit_queue_progress_callback_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_history(monkeypatch)
    monkeypatch.setattr(
        bulk_ingestion, "submit_manifest",
        lambda *_a, **_kw: _ok_result(),
    )
    events: list[tuple[int, str]] = []
    items = [QueueItem(label="ok", raw_text=_good_manifest_text(), source="pasted")]
    validations = bulk_ingestion.validate_queue(items)
    list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["o"], acl_viewers=["v"],
            legal_tag="L", data_partition_id="opendes",
            connection=_connection(), token="t",
            sleeper=lambda _s: None,
            progress_callback=lambda idx, state, **_kw: events.append((idx, state)),
        )
    )
    assert (0, "submitting") in events
    assert (0, "success") in events


def test_submit_queue_progress_callback_exception_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_history(monkeypatch)
    monkeypatch.setattr(
        bulk_ingestion, "submit_manifest",
        lambda *_a, **_kw: _ok_result(),
    )
    items = [QueueItem(label="ok", raw_text=_good_manifest_text(), source="pasted")]
    validations = bulk_ingestion.validate_queue(items)

    def boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("UI on fire")

    results = list(
        bulk_ingestion.submit_queue(
            items, validations,
            acl_owners=["o"], acl_viewers=["v"],
            legal_tag="L", data_partition_id="opendes",
            connection=_connection(), token="t",
            sleeper=lambda _s: None,
            progress_callback=boom,
        )
    )
    assert results[0].status == "success"  # loop survived


# ---------------------------------------------------------------------------
# submit_queue — argument validation
# ---------------------------------------------------------------------------


def test_submit_queue_rejects_mismatched_lengths() -> None:
    items = [QueueItem(label="a", raw_text="{}", source="pasted")]
    validations: list[QueueValidationResult] = []
    with pytest.raises(ValueError, match="same length"):
        list(
            bulk_ingestion.submit_queue(
                items, validations,
                acl_owners=["o"], acl_viewers=["v"],
                legal_tag="L", data_partition_id="opendes",
                connection=_connection(), token="t",
            )
        )
