"""ADME manifest-ingestion service calls.

Sibling to :mod:`app.services.entitlements`: stdlib + ``requests`` only,
a 5-second per-call timeout (30 s for the submit endpoint, which kicks
off DAG creation server-side), and *no internal retries* — the Streamlit
page owns polling and re-run UX. Each public function returns a frozen
result dataclass from :mod:`app.models.osdu` describing exactly what
happened, including a server-supplied correlation identifier when one
is present.

This module also owns the canonical TNO sample manifest constant and
the pure ``substitute_manifest_placeholders`` / ``validate_manifest_json``
helpers used by the page before any HTTP work.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from time import perf_counter
from urllib.parse import quote

import requests  # type: ignore[import-untyped]

from app.models.connection import ADMEConnection
from app.models.osdu import (
    LegalTagCheckResult,
    StorageRecordsResult,
    WorkflowRunResult,
    WorkflowStatus,
    parse_workflow_status,
)
from app.services.legal_tags import LEGAL_TAG_VALIDATE_PATH, LEGAL_TAGS_PATH

logger = logging.getLogger(__name__)

INGESTION_TIMEOUT_SECONDS = 30

# ``LEGAL_TAGS_PATH`` is owned by :mod:`app.services.legal_tags` and
# re-exported here so existing callers keep working. Single source of
# truth lives in the legal_tags module.
__all__ = [
    "INGESTION_TIMEOUT_SECONDS",
    "LEGAL_TAGS_PATH",
    "SAMPLE_PLACEHOLDER_ACL_OWNERS",
    "SAMPLE_PLACEHOLDER_ACL_VIEWERS",
    "SAMPLE_PLACEHOLDER_DATA_PARTITION_ID",
    "SAMPLE_PLACEHOLDER_LEGAL_TAG",
    "TNO_SAMPLE_DESCRIPTION",
    "TNO_SAMPLE_MANIFEST",
    "STORAGE_MAX_RECORDS_PER_CALL",
    "STORAGE_RECORDS_PATH",
    "WORKFLOW_INGEST_RUN_PATH",
    "WORKFLOW_RUN_STATUS_PATH_TEMPLATE",
    "check_legal_tag",
    "get_workflow_status",
    "put_records",
    "submit_manifest",
    "substitute_manifest_placeholders",
    "validate_manifest_json",
]

WORKFLOW_INGEST_RUN_PATH = (
    "/api/workflow/v1/workflow/Osdu_ingest/workflowRun"
)
WORKFLOW_RUN_STATUS_PATH_TEMPLATE = (
    "/api/workflow/v1/workflow/Osdu_ingest/workflowRun/{run_id}"
)
# OSDU Storage service: create-or-update records (PUT accepts up to 500
# records per call). Used by the Storage-API bulk load path.
STORAGE_RECORDS_PATH = "/api/storage/v2/records"
STORAGE_MAX_RECORDS_PER_CALL = 500

# Placeholder tokens substituted by ``substitute_manifest_placeholders``.
SAMPLE_PLACEHOLDER_DATA_PARTITION_ID = "{{DATA_PARTITION_ID}}"
SAMPLE_PLACEHOLDER_LEGAL_TAG = "{{LEGAL_TAG_NAME}}"
SAMPLE_PLACEHOLDER_ACL_OWNERS = "{{ACL_OWNERS}}"
SAMPLE_PLACEHOLDER_ACL_VIEWERS = "{{ACL_VIEWERS}}"

# Short human-readable description of what the TNO sample loads.
TNO_SAMPLE_DESCRIPTION = (
    "Loads one OSDU reference-data record "
    "(AliasNameType:Borehole) end-to-end through the Osdu_ingest "
    "workflow. No file upload, no parent records — the smallest "
    "possible proof that workflow, schema, storage, indexer, and "
    "search are all wired up for the current partition."
)

# Canonical TNO sample manifest. Sourced by Darryl from the Azure
# osdu-data-load-tno v0.0.10 README. Placeholders are replaced by
# ``substitute_manifest_placeholders`` before submit.
TNO_SAMPLE_MANIFEST: str = """{
  "executionContext": {
    "Payload": {
      "AppKey": "adme-ingestion-tool",
      "data-partition-id": "{{DATA_PARTITION_ID}}"
    },
    "manifest": {
      "kind": "osdu:wks:Manifest:1.0.0",
      "ReferenceData": [
        {
          "id": "{{DATA_PARTITION_ID}}:reference-data--AliasNameType:Borehole",
          "kind": "osdu:wks:reference-data--AliasNameType:1.0.0",
          "acl": {
            "viewers": ["{{ACL_VIEWERS}}"],
            "owners": ["{{ACL_OWNERS}}"]
          },
          "legal": {
            "legaltags": ["{{LEGAL_TAG_NAME}}"],
            "otherRelevantDataCountries": ["US"],
            "status": "compliant"
          },
          "data": {
            "Source": "TNO",
            "Name": "Borehole",
            "Code": "Borehole"
          }
        }
      ]
    }
  }
}"""

_CORRELATION_HEADER_NAMES: tuple[str, ...] = (
    "correlation-id",
    "x-correlation-id",
    "request-id",
    "x-request-id",
)

_ERROR_BODY_TEXT_LIMIT = 500

_MANIFEST_SECTION_KEYS: tuple[str, ...] = (
    "ReferenceData",
    "MasterData",
    "Data",
)


def substitute_manifest_placeholders(
    template: str,
    *,
    data_partition_id: str,
    legal_tag_name: str,
    acl_owners: str,
    acl_viewers: str,
) -> str:
    """Substitute the four placeholder tokens in ``template``.

    Pure string replacement; does NOT parse JSON. Raises ``ValueError``
    when any input is blank/whitespace-only or when an unresolved
    ``{{...}}`` token remains after substitution.
    """
    partition = data_partition_id.strip()
    legal = legal_tag_name.strip()
    owners = acl_owners.strip()
    viewers = acl_viewers.strip()

    if not partition:
        raise ValueError("data_partition_id is required.")
    if not legal:
        raise ValueError("legal_tag_name is required.")
    if not owners:
        raise ValueError("acl_owners is required.")
    if not viewers:
        raise ValueError("acl_viewers is required.")
    if not template or not template.strip():
        raise ValueError("template is required.")

    rendered = (
        template.replace(SAMPLE_PLACEHOLDER_DATA_PARTITION_ID, partition)
        .replace(SAMPLE_PLACEHOLDER_LEGAL_TAG, legal)
        .replace(SAMPLE_PLACEHOLDER_ACL_OWNERS, owners)
        .replace(SAMPLE_PLACEHOLDER_ACL_VIEWERS, viewers)
    )

    if "{{" in rendered:
        raise ValueError(
            "Manifest still contains unresolved {{...}} tokens after "
            "substitution."
        )

    return rendered


def validate_manifest_json(
    text: str,
) -> tuple[bool, str, dict | None]:
    """Validate that ``text`` is a well-formed OSDU ingest manifest.

    Pure function: no HTTP, no I/O. Returns ``(ok, error_message,
    parsed)``. The first failing rule wins; on full success the parsed
    dict is returned and ``error_message`` is empty.
    """
    if text is None or not text.strip():
        return False, "Manifest is empty.", None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, f"Manifest is not valid JSON: {exc}", None

    if not isinstance(parsed, dict):
        return False, "Manifest top-level must be a JSON object.", None

    execution_context = parsed.get("executionContext")
    if not isinstance(execution_context, dict):
        return False, "Manifest is missing 'executionContext'.", None

    manifest = execution_context.get("manifest")
    if not isinstance(manifest, dict):
        return (
            False,
            "Manifest is missing 'executionContext.manifest'.",
            None,
        )

    # Sections to validate as flat lists of records. ``Data`` is
    # special-cased below because the canonical Work-Product-Component
    # shape emitted by ``app.services.manifest_builder`` is an *object*
    # containing ``WorkProductComponents`` / ``WorkProduct`` /
    # ``Datasets`` rather than a flat list.
    present_sections: list[tuple[str, list]] = []
    has_any_section = False
    for key in _MANIFEST_SECTION_KEYS:
        if key not in manifest:
            continue
        has_any_section = True
        value = manifest[key]
        if key == "Data":
            if isinstance(value, list):
                present_sections.append((key, value))
                continue
            if isinstance(value, dict):
                ok, message = _validate_data_object(value)
                if not ok:
                    return False, message, None
                # Per-record validation is handled inside
                # ``_validate_data_object`` for Datasets and
                # WorkProductComponents.
                continue
            return (
                False,
                (
                    "Manifest section 'Data' must be a list of "
                    "records or an object with Datasets/"
                    "WorkProductComponents/WorkProduct."
                ),
                None,
            )
        if not isinstance(value, list):
            return (
                False,
                f"Manifest section '{key}' must be a list.",
                None,
            )
        present_sections.append((key, value))

    if not has_any_section:
        return (
            False,
            (
                "Manifest must contain at least one of ReferenceData, "
                "MasterData, or Data."
            ),
            None,
        )

    for section_key, items in present_sections:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                return (
                    False,
                    (
                        f"Manifest item at {section_key}[{index}] "
                        "must be a JSON object."
                    ),
                    None,
                )
            kind = item.get("kind")
            if not isinstance(kind, str) or not kind.strip():
                return (
                    False,
                    (
                        f"Manifest item at {section_key}[{index}] "
                        "is missing a string 'kind'."
                    ),
                    None,
                )

    return True, "", parsed


def _validate_data_object(data_obj: dict) -> tuple[bool, str]:
    """Validate the WPC-shaped ``Data`` object.

    Accepts ``Datasets`` (list of record dicts), ``WorkProductComponents``
    (list of record dicts), and ``WorkProduct`` (record dict). Each
    record dict must carry a non-empty string ``kind``. Unknown keys
    are allowed for forward compatibility.
    """
    list_subkeys = ("Datasets", "WorkProductComponents")
    for subkey in list_subkeys:
        if subkey not in data_obj:
            continue
        sub_value = data_obj[subkey]
        if not isinstance(sub_value, list):
            return (
                False,
                f"Manifest section 'Data.{subkey}' must be a list.",
            )
        for index, item in enumerate(sub_value):
            if not isinstance(item, dict):
                return (
                    False,
                    (
                        f"Manifest item at Data.{subkey}[{index}] "
                        "must be a JSON object."
                    ),
                )
            kind = item.get("kind")
            if not isinstance(kind, str) or not kind.strip():
                return (
                    False,
                    (
                        f"Manifest item at Data.{subkey}[{index}] "
                        "is missing a string 'kind'."
                    ),
                )

    if "WorkProduct" in data_obj:
        wp = data_obj["WorkProduct"]
        if not isinstance(wp, dict):
            return (
                False,
                "Manifest section 'Data.WorkProduct' must be an object.",
            )

    return True, ""


def check_legal_tag(
    connection: ADMEConnection,
    token: str,
    legal_tag_name: str,
) -> LegalTagCheckResult:
    """Validate a legal tag via ``POST /api/legal/v1/legaltags:validate``.

    Returns a :class:`LegalTagCheckResult`. The validate endpoint returns
    ``{"invalidLegalTags": [...]}``; the tag is valid when it does NOT
    appear in that list. Transport failures return ``ok=False`` with
    ``http_status=None`` and never raise.
    """
    if not legal_tag_name or not legal_tag_name.strip():
        raise ValueError(
            "A non-empty legal tag name is required for the "
            "legal-tag check."
        )

    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_legal(
            connection=connection,
            token=token,
            method="POST",
            path=LEGAL_TAG_VALIDATE_PATH,
            json_body={"names": [legal_tag_name]},
        )
    )

    if http_status is None:
        return LegalTagCheckResult(
            name=legal_tag_name,
            ok=False,
            http_status=None,
            latency_ms=latency_ms,
            correlation_id=None,
            error_message=error_message,
        )

    if 200 <= http_status < 300:
        body = parsed_body if isinstance(parsed_body, dict) else {}
        invalid_tags = body.get("invalidLegalTags", [])
        invalid_names = [
            entry.get("name") if isinstance(entry, dict) else entry
            for entry in invalid_tags
        ] if isinstance(invalid_tags, list) else []
        if legal_tag_name in invalid_names:
            friendly = (
                f"Legal tag '{legal_tag_name}' is not valid in "
                f"partition '{connection.data_partition_id}'."
            )
            return LegalTagCheckResult(
                name=legal_tag_name,
                ok=False,
                http_status=http_status,
                latency_ms=latency_ms,
                correlation_id=correlation_id,
                error_message=friendly,
            )
        return LegalTagCheckResult(
            name=legal_tag_name,
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            error_message=None,
        )

    return LegalTagCheckResult(
        name=legal_tag_name,
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        correlation_id=correlation_id,
        error_message=error_message,
    )


def submit_manifest(
    connection: ADMEConnection,
    token: str,
    manifest_payload: dict,
) -> WorkflowRunResult:
    """POST a manifest to ``Osdu_ingest`` and return the run handle.

    The caller is responsible for having shaped ``manifest_payload``
    correctly (top-level ``executionContext`` etc.); this function does
    no rewrapping. A 2xx response missing ``runId`` is surfaced as
    ``ok=False`` with a curated message.
    """
    if not isinstance(manifest_payload, dict) or not manifest_payload:
        raise ValueError(
            "A non-empty manifest_payload dict is required for "
            "submit_manifest."
        )

    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_workflow(
            connection=connection,
            token=token,
            method="POST",
            path=WORKFLOW_INGEST_RUN_PATH,
            json_body=manifest_payload,
        )
    )

    if http_status is None:
        return WorkflowRunResult(
            workflow_id=None,
            run_id=None,
            status=WorkflowStatus.UNKNOWN,
            raw_status="",
            message=None,
            ok=False,
            http_status=None,
            latency_ms=latency_ms,
            correlation_id=None,
            error_message=error_message,
            raw_response=None,
        )

    if 200 <= http_status < 300:
        body = parsed_body if isinstance(parsed_body, dict) else {}
        run_id_raw = body.get("runId")
        run_id = run_id_raw.strip() if isinstance(run_id_raw, str) else ""
        workflow_id_raw = body.get("workflowId")
        workflow_id = (
            workflow_id_raw
            if isinstance(workflow_id_raw, str) and workflow_id_raw
            else None
        )
        raw_status_raw = body.get("status", "")
        raw_status = raw_status_raw if isinstance(raw_status_raw, str) else ""
        message_raw = body.get("message")
        message = (
            message_raw
            if isinstance(message_raw, str) and message_raw
            else None
        )
        normalized_status = parse_workflow_status(raw_status)

        if not run_id:
            return WorkflowRunResult(
                workflow_id=workflow_id,
                run_id=None,
                status=WorkflowStatus.UNKNOWN,
                raw_status=raw_status,
                message=message,
                ok=False,
                http_status=http_status,
                latency_ms=latency_ms,
                correlation_id=correlation_id,
                error_message=(
                    "Workflow accepted the request but returned no "
                    "runId."
                ),
                raw_response=parsed_body,
            )

        return WorkflowRunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=normalized_status,
            raw_status=raw_status,
            message=message,
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            error_message=None,
            raw_response=parsed_body,
        )

    return WorkflowRunResult(
        workflow_id=None,
        run_id=None,
        status=WorkflowStatus.UNKNOWN,
        raw_status="",
        message=None,
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        correlation_id=correlation_id,
        error_message=error_message,
        raw_response=parsed_body,
    )


def put_records(
    connection: ADMEConnection,
    token: str,
    records: list[dict],
) -> StorageRecordsResult:
    """Create or update a batch of records via ``PUT /storage/v2/records``.

    Bypasses the ingestion workflow/DAG entirely: the records are written
    straight to the Storage service, which validates ACL/legal/schema and
    persists them synchronously. ``records`` must already be storage-shaped
    (``id``, ``kind``, ``acl``, ``legal``, ``data``) and number no more than
    :data:`STORAGE_MAX_RECORDS_PER_CALL` (500). The Storage response body
    (``recordCount`` / ``recordIds`` / ``skippedRecordIds``) is surfaced on
    the result.
    """
    if not isinstance(records, list) or not records:
        raise ValueError("A non-empty list of records is required.")
    if len(records) > STORAGE_MAX_RECORDS_PER_CALL:
        raise ValueError(
            f"A single Storage call accepts at most "
            f"{STORAGE_MAX_RECORDS_PER_CALL} records; got {len(records)}."
        )

    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call(
            connection=connection,
            token=token,
            method="PUT",
            path=STORAGE_RECORDS_PATH,
            json_body=records,
        )
    )

    body = parsed_body if isinstance(parsed_body, dict) else {}
    record_ids_raw = body.get("recordIds")
    record_ids = (
        [r for r in record_ids_raw if isinstance(r, str)]
        if isinstance(record_ids_raw, list)
        else []
    )
    skipped_raw = body.get("skippedRecordIds")
    skipped = (
        [r for r in skipped_raw if isinstance(r, str)]
        if isinstance(skipped_raw, list)
        else []
    )
    count_raw = body.get("recordCount")
    record_count = (
        count_raw if isinstance(count_raw, int) else len(record_ids)
    )
    ok = http_status is not None and 200 <= http_status < 300

    return StorageRecordsResult(
        ok=ok,
        http_status=http_status,
        record_count=record_count,
        record_ids=record_ids,
        skipped_record_ids=skipped,
        error_message=None if ok else error_message,
        correlation_id=correlation_id,
        latency_ms=latency_ms,
    )


def get_workflow_status(
    connection: ADMEConnection,
    token: str,
    run_id: str,
) -> WorkflowRunResult:
    """Probe a single workflow run's status.

    Single call, no internal polling, no sleeping. The page drives the
    polling cadence. Non-2xx responses surface as ``ok=False`` and the
    caller decides whether to retry.
    """
    if not run_id or not run_id.strip():
        raise ValueError(
            "A non-empty run_id is required for get_workflow_status."
        )

    quoted_run_id = quote(run_id, safe="")
    path = WORKFLOW_RUN_STATUS_PATH_TEMPLATE.format(run_id=quoted_run_id)

    parsed_body, http_status, correlation_id, latency_ms, error_message = (
        _call_workflow(
            connection=connection,
            token=token,
            method="GET",
            path=path,
            json_body=None,
        )
    )

    if http_status is None:
        return WorkflowRunResult(
            workflow_id=None,
            run_id=run_id,
            status=WorkflowStatus.UNKNOWN,
            raw_status="",
            message=None,
            ok=False,
            http_status=None,
            latency_ms=latency_ms,
            correlation_id=None,
            error_message=error_message,
            raw_response=None,
        )

    if 200 <= http_status < 300:
        body = parsed_body if isinstance(parsed_body, dict) else {}
        echoed_run_id_raw = body.get("runId")
        echoed_run_id = (
            echoed_run_id_raw
            if isinstance(echoed_run_id_raw, str) and echoed_run_id_raw
            else run_id
        )
        workflow_id_raw = body.get("workflowId")
        workflow_id = (
            workflow_id_raw
            if isinstance(workflow_id_raw, str) and workflow_id_raw
            else None
        )
        raw_status_raw = body.get("status", "")
        raw_status = raw_status_raw if isinstance(raw_status_raw, str) else ""
        message_raw = body.get("message")
        message = (
            message_raw
            if isinstance(message_raw, str) and message_raw
            else None
        )
        return WorkflowRunResult(
            workflow_id=workflow_id,
            run_id=echoed_run_id,
            status=parse_workflow_status(raw_status),
            raw_status=raw_status,
            message=message,
            ok=True,
            http_status=http_status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            error_message=None,
            raw_response=parsed_body,
        )

    return WorkflowRunResult(
        workflow_id=None,
        run_id=run_id,
        status=WorkflowStatus.UNKNOWN,
        raw_status="",
        message=None,
        ok=False,
        http_status=http_status,
        latency_ms=latency_ms,
        correlation_id=correlation_id,
        error_message=error_message,
        raw_response=parsed_body,
    )


def _call_workflow(
    connection: ADMEConnection,
    token: str,
    *,
    method: str,
    path: str,
    json_body: dict | None,
) -> tuple[dict | str | None, int | None, str | None, float, str | None]:
    """Shared HTTP wrapper for the workflow service calls."""
    return _call(
        connection=connection,
        token=token,
        method=method,
        path=path,
        json_body=json_body,
    )


def _call_legal(
    connection: ADMEConnection,
    token: str,
    path: str,
    *,
    method: str = "GET",
    json_body: dict | None = None,
) -> tuple[dict | str | None, int | None, str | None, float, str | None]:
    """Shared HTTP wrapper for legal service calls."""
    return _call(
        connection=connection,
        token=token,
        method=method,
        path=path,
        json_body=json_body,
    )


def _call(
    connection: ADMEConnection,
    token: str,
    *,
    method: str,
    path: str,
    json_body: dict | list | None,
) -> tuple[dict | str | None, int | None, str | None, float, str | None]:
    if not connection.is_valid():
        raise ValueError(
            "ADME connection is incomplete. Endpoint, tenant ID, "
            "client ID, and data partition ID are required. Service "
            "principal auth also requires a client secret."
        )
    if not token.strip():
        raise ValueError(
            "A non-empty bearer token is required for ingestion calls."
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
                timeout=INGESTION_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        elif method == "POST":
            response = requests.post(
                url=url,
                headers=headers,
                json=json_body,
                timeout=INGESTION_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        elif method == "PUT":
            response = requests.put(
                url=url,
                headers=headers,
                json=json_body,
                timeout=INGESTION_TIMEOUT_SECONDS,
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
            f"Request timed out after {INGESTION_TIMEOUT_SECONDS}s",
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
