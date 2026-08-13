"""Tests for ``app.services.ingestion``: validation + 3 HTTP probes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import requests  # type: ignore[import-untyped]

from app.models.connection import ADMEConnection, AuthMethod
from app.models.osdu import (
    LegalTagCheckResult,
    WorkflowRunResult,
    WorkflowStatus,
)
from app.services import ingestion as ingestion_module
from app.services.ingestion import (
    INGESTION_TIMEOUT_SECONDS,
    STORAGE_MAX_RECORDS_PER_CALL,
    STORAGE_RECORDS_PATH,
    TNO_SAMPLE_MANIFEST,
    WORKFLOW_INGEST_RUN_PATH,
    WORKFLOW_RUN_STATUS_PATH_TEMPLATE,
    check_legal_tag,
    get_workflow_status,
    put_records,
    submit_manifest,
    substitute_manifest_placeholders,
    validate_manifest_json,
)
from app.services.legal_tags import LEGAL_TAG_VALIDATE_PATH

# ---------------------------------------------------------------------------
# Helpers
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


def _patch_get(
    monkeypatch: pytest.MonkeyPatch,
    response_factory: Any,
) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake_get(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return response_factory(**kwargs)

    monkeypatch.setattr(ingestion_module.requests, "get", fake_get)
    return captured


def _patch_post(
    monkeypatch: pytest.MonkeyPatch,
    response_factory: Any,
) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake_post(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return response_factory(**kwargs)

    monkeypatch.setattr(ingestion_module.requests, "post", fake_post)
    return captured


def _patch_put(
    monkeypatch: pytest.MonkeyPatch,
    response_factory: Any,
) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake_put(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return response_factory(**kwargs)

    monkeypatch.setattr(ingestion_module.requests, "put", fake_put)
    return captured


_VALID_MANIFEST: dict[str, Any] = {
    "executionContext": {
        "manifest": {
            "ReferenceData": [
                {"kind": "osdu:wks:reference-data--AliasNameType:1.0.0"}
            ]
        }
    }
}


# ===========================================================================
# validate_manifest_json
# ===========================================================================


def test_validate_manifest_json_happy_path_all_three_sections() -> None:
    text = (
        '{"executionContext": {"manifest": {'
        '"ReferenceData": [{"kind": "k1"}],'
        '"MasterData": [{"kind": "k2"}],'
        '"Data": [{"kind": "k3"}]'
        "}}}"
    )
    ok, error_message, parsed = validate_manifest_json(text)
    assert ok is True
    assert error_message == ""
    assert parsed is not None
    assert "executionContext" in parsed


def test_validate_manifest_json_accepts_substituted_tno_sample() -> None:
    """The shipped TNO sample, after substitution, must validate."""
    rendered = substitute_manifest_placeholders(
        TNO_SAMPLE_MANIFEST,
        data_partition_id="opendes",
        legal_tag_name="opendes-open-test",
        acl_owners="data.default.owners@opendes",
        acl_viewers="data.default.viewers@opendes",
    )
    ok, error_message, parsed = validate_manifest_json(rendered)
    assert ok is True, error_message
    assert parsed is not None
    ref_data = parsed["executionContext"]["manifest"]["ReferenceData"]
    assert len(ref_data) == 1


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_validate_manifest_json_rejects_blank(text: str) -> None:
    ok, message, parsed = validate_manifest_json(text)
    assert ok is False
    assert "empty" in message.lower()
    assert parsed is None


def test_validate_manifest_json_rejects_invalid_json() -> None:
    ok, message, parsed = validate_manifest_json("{not json")
    assert ok is False
    assert "valid JSON" in message
    assert parsed is None


def test_validate_manifest_json_rejects_top_level_array() -> None:
    ok, message, parsed = validate_manifest_json("[1, 2, 3]")
    assert ok is False
    assert "object" in message.lower()
    assert parsed is None


def test_validate_manifest_json_rejects_missing_execution_context() -> None:
    ok, message, parsed = validate_manifest_json('{"foo": "bar"}')
    assert ok is False
    assert "executionContext" in message
    assert parsed is None


def test_validate_manifest_json_rejects_missing_manifest() -> None:
    ok, message, parsed = validate_manifest_json(
        '{"executionContext": {"Payload": {}}}'
    )
    assert ok is False
    assert "executionContext.manifest" in message
    assert parsed is None


def test_validate_manifest_json_rejects_no_entity_arrays() -> None:
    ok, message, parsed = validate_manifest_json(
        '{"executionContext": {"manifest": {"kind": "x"}}}'
    )
    assert ok is False
    assert "ReferenceData" in message
    assert parsed is None


def test_validate_manifest_json_rejects_section_that_is_not_a_list() -> None:
    ok, message, parsed = validate_manifest_json(
        '{"executionContext": {"manifest": {"ReferenceData": {}}}}'
    )
    assert ok is False
    assert "must be a list" in message
    assert parsed is None


def test_validate_manifest_json_rejects_item_missing_kind() -> None:
    ok, message, parsed = validate_manifest_json(
        '{"executionContext": {"manifest": '
        '{"ReferenceData": [{"foo": "bar"}]}}}'
    )
    assert ok is False
    assert "kind" in message
    assert parsed is None


def test_validate_manifest_json_rejects_item_with_non_string_kind() -> None:
    ok, message, parsed = validate_manifest_json(
        '{"executionContext": {"manifest": '
        '{"ReferenceData": [{"kind": 7}]}}}'
    )
    assert ok is False
    assert "kind" in message
    assert parsed is None


# ---------------------------------------------------------------------------
# validate_manifest_json — Data section: list AND object (WPC) shapes
# ---------------------------------------------------------------------------


def _wrap_manifest(manifest_body: str) -> str:
    return (
        '{"executionContext": {"manifest": '
        + manifest_body
        + "}}"
    )


def test_validate_manifest_json_accepts_legacy_data_list_shape() -> None:
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest('{"Data": [{"kind": "osdu:wks:foo:1.0.0"}]}')
    )
    assert ok is True, message
    assert message == ""
    assert parsed is not None


def test_validate_manifest_json_accepts_data_object_with_datasets() -> None:
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest(
            '{"Data": {"Datasets": '
            '[{"kind": "osdu:wks:dataset--File.Generic:1.0.0"}]}}'
        )
    )
    assert ok is True, message
    assert parsed is not None


def test_validate_manifest_json_accepts_data_object_with_wpcs() -> None:
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest(
            '{"Data": {"WorkProductComponents": '
            '[{"kind": "osdu:wks:work-product-component--Generic:1.0.0"}]}}'
        )
    )
    assert ok is True, message
    assert parsed is not None


def test_validate_manifest_json_accepts_data_object_with_workproduct() -> None:
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest(
            '{"Data": {"WorkProduct": '
            '{"kind": "osdu:wks:work-product--Generic:1.0.0"}}}'
        )
    )
    assert ok is True, message
    assert parsed is not None


def test_validate_manifest_json_accepts_data_object_empty_workproduct() -> (
    None
):
    """Builder emits ``WorkProduct: {}`` when there's no parent product."""
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest(
            '{"Data": {"WorkProduct": {}, "Datasets": '
            '[{"kind": "osdu:wks:dataset--File.Generic:1.0.0"}]}}'
        )
    )
    assert ok is True, message
    assert parsed is not None


def test_validate_manifest_json_allows_unknown_keys_in_data_object() -> None:
    """Forward compat: unknown keys inside Data are allowed."""
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest(
            '{"Data": {"Datasets": '
            '[{"kind": "osdu:wks:dataset--File.Generic:1.0.0"}], '
            '"FutureField": 42}}'
        )
    )
    assert ok is True, message
    assert parsed is not None


def test_validate_manifest_json_rejects_data_object_datasets_not_list() -> (
    None
):
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest('{"Data": {"Datasets": {"kind": "x"}}}')
    )
    assert ok is False
    assert "Data.Datasets" in message
    assert "must be a list" in message
    assert parsed is None


def test_validate_manifest_json_rejects_data_object_wpcs_not_list() -> None:
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest('{"Data": {"WorkProductComponents": "nope"}}')
    )
    assert ok is False
    assert "Data.WorkProductComponents" in message
    assert "must be a list" in message
    assert parsed is None


def test_validate_manifest_json_rejects_data_object_workproduct_not_dict() -> (
    None
):
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest('{"Data": {"WorkProduct": [1, 2]}}')
    )
    assert ok is False
    assert "Data.WorkProduct" in message
    assert "must be an object" in message
    assert parsed is None


def test_validate_manifest_json_rejects_data_object_dataset_missing_kind() -> (
    None
):
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest(
            '{"Data": {"Datasets": [{"id": "x"}]}}'
        )
    )
    assert ok is False
    assert "Data.Datasets[0]" in message
    assert "kind" in message
    assert parsed is None


def test_validate_manifest_json_rejects_data_as_string() -> None:
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest('{"Data": "not a list or object"}')
    )
    assert ok is False
    assert "Data" in message
    assert "list of records" in message
    assert "Datasets" in message
    assert parsed is None


def test_validate_manifest_json_referencedata_still_list_only() -> None:
    """The Data loosening must not affect ReferenceData."""
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest('{"ReferenceData": {"Datasets": []}}')
    )
    assert ok is False
    assert "ReferenceData" in message
    assert "must be a list" in message
    assert parsed is None


def test_validate_manifest_json_masterdata_still_list_only() -> None:
    """The Data loosening must not affect MasterData."""
    ok, message, parsed = validate_manifest_json(
        _wrap_manifest('{"MasterData": {"foo": "bar"}}')
    )
    assert ok is False
    assert "MasterData" in message
    assert "must be a list" in message
    assert parsed is None


def test_validate_manifest_json_round_trips_builder_output() -> None:
    """Builder output must round-trip cleanly through the validator."""
    import json as _json

    from app.services.manifest_builder import build_file_generic_manifest

    manifest = build_file_generic_manifest(
        file_source="/path/to/uploaded/file.las",
        file_id="opendes:dataset--File.Generic:abc123",
        display_name="Sample LAS file",
        description="Round-trip smoke test",
        kind="osdu:wks:dataset--File.Generic:1.0.0",
        legal_tag="opendes-open-test",
        acl_owners="data.default.owners@opendes.contoso.com",
        acl_viewers="data.default.viewers@opendes.contoso.com",
        data_partition_id="opendes",
    )
    text = _json.dumps(manifest)
    ok, message, parsed = validate_manifest_json(text)
    assert ok is True, message
    assert message == ""
    assert parsed is not None
    # Sanity: confirm Builder really emits Data as object (the case
    # that motivated the loosening).
    assert isinstance(
        parsed["executionContext"]["manifest"]["Data"], dict
    )


# ===========================================================================
# substitute_manifest_placeholders
# ===========================================================================


def test_substitute_manifest_placeholders_happy_path() -> None:
    rendered = substitute_manifest_placeholders(
        TNO_SAMPLE_MANIFEST,
        data_partition_id="opendes",
        legal_tag_name="opendes-open-test",
        acl_owners="owners@opendes",
        acl_viewers="viewers@opendes",
    )
    assert "{{" not in rendered
    assert "opendes" in rendered
    assert "opendes-open-test" in rendered
    assert "owners@opendes" in rendered
    assert "viewers@opendes" in rendered


@pytest.mark.parametrize(
    "kwargs",
    [
        {"data_partition_id": "  "},
        {"legal_tag_name": ""},
        {"acl_owners": "\t"},
        {"acl_viewers": ""},
    ],
)
def test_substitute_manifest_placeholders_rejects_blank_inputs(
    kwargs: dict[str, str],
) -> None:
    base = {
        "data_partition_id": "p",
        "legal_tag_name": "l",
        "acl_owners": "o",
        "acl_viewers": "v",
    }
    base.update(kwargs)
    with pytest.raises(ValueError):
        substitute_manifest_placeholders(TNO_SAMPLE_MANIFEST, **base)


def test_substitute_manifest_placeholders_rejects_unresolved_token() -> None:
    template = (
        '{"data": "{{LEGAL_TAG_NAME}}", "extra": "{{NEW_TOKEN}}"}'
    )
    with pytest.raises(ValueError, match="unresolved"):
        substitute_manifest_placeholders(
            template,
            data_partition_id="p",
            legal_tag_name="l",
            acl_owners="o",
            acl_viewers="v",
        )


# ===========================================================================
# check_legal_tag
# ===========================================================================


def test_check_legal_tag_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-legal-1"},
            json_payload={"invalidLegalTags": []},
        ),
    )

    result = check_legal_tag(
        _connection(), token="t", legal_tag_name="opendes-open-test"
    )

    assert isinstance(result, LegalTagCheckResult)
    assert result.ok is True
    assert result.http_status == 200
    assert result.name == "opendes-open-test"
    assert result.correlation_id == "corr-legal-1"
    assert result.error_message is None
    assert result.latency_ms >= 0.0
    assert len(captured) == 1
    assert captured[0]["url"].endswith(LEGAL_TAG_VALIDATE_PATH)
    assert captured[0]["json"] == {"names": ["opendes-open-test"]}


def test_check_legal_tag_invalid_tag_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-invalid"},
            json_payload={
                "invalidLegalTags": [
                    {"name": "missing-tag", "reason": "not found"}
                ]
            },
        ),
    )

    result = check_legal_tag(
        _connection(data_partition_id="opendes"),
        token="t",
        legal_tag_name="missing-tag",
    )

    assert result.ok is False
    assert result.http_status == 200
    assert result.error_message is not None
    assert "missing-tag" in result.error_message
    assert "opendes" in result.error_message
    assert "not valid" in result.error_message.lower()
    assert result.correlation_id == "corr-invalid"


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_check_legal_tag_http_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=status_code,
            headers={"correlation-id": f"corr-{status_code}"},
            json_payload={"message": f"boom {status_code}"},
        ),
    )

    result = check_legal_tag(_connection(), token="t", legal_tag_name="x")

    assert result.ok is False
    assert result.http_status == status_code
    assert result.error_message is not None
    assert f"boom {status_code}" in result.error_message
    assert result.correlation_id == f"corr-{status_code}"


def test_check_legal_tag_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.Timeout("read timed out")

    monkeypatch.setattr(ingestion_module.requests, "post", fake_post)

    result = check_legal_tag(_connection(), token="t", legal_tag_name="x")

    assert result.ok is False
    assert result.http_status is None
    assert result.error_message is not None
    assert "timed out" in result.error_message.lower()
    assert str(INGESTION_TIMEOUT_SECONDS) in result.error_message


def test_check_legal_tag_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.ConnectionError("dns failure")

    monkeypatch.setattr(ingestion_module.requests, "post", fake_post)

    result = check_legal_tag(_connection(), token="t", legal_tag_name="x")

    assert result.ok is False
    assert result.http_status is None
    assert result.error_message is not None
    assert "ConnectionError" in result.error_message


def test_check_legal_tag_outgoing_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={"invalidLegalTags": []},
        ),
    )

    check_legal_tag(_connection(), token="bearer-abc", legal_tag_name="x")

    headers = captured[0]["headers"]
    assert headers["Authorization"] == "Bearer bearer-abc"
    assert headers["data-partition-id"] == "example-opendes"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"  # POST — has body
    assert captured[0]["timeout"] == INGESTION_TIMEOUT_SECONDS
    assert captured[0]["allow_redirects"] is False


@pytest.mark.parametrize(
    "header_name", ["correlation-id", "X-Correlation-ID", "Request-Id", "X-Request-Id"]
)
def test_check_legal_tag_correlation_id_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, header_name: str
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={header_name: "corr-x"},
            json_payload={"invalidLegalTags": []},
        ),
    )

    result = check_legal_tag(_connection(), token="t", legal_tag_name="x")
    assert result.correlation_id == "corr-x"


def test_check_legal_tag_sends_name_in_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={"invalidLegalTags": []},
        ),
    )

    weird = "a tag/with spaces+and-slash"
    check_legal_tag(_connection(), token="t", legal_tag_name=weird)

    assert captured[0]["json"] == {"names": [weird]}
    assert captured[0]["url"].endswith(LEGAL_TAG_VALIDATE_PATH)


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_check_legal_tag_rejects_blank_name(name: str) -> None:
    with pytest.raises(ValueError, match="legal tag name"):
        check_legal_tag(_connection(), token="t", legal_tag_name=name)


@pytest.mark.parametrize("token", ["", "   "])
def test_check_legal_tag_rejects_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="non-empty bearer token"):
        check_legal_tag(_connection(), token=token, legal_tag_name="x")


def test_check_legal_tag_rejects_invalid_connection() -> None:
    bad = ADMEConnection(
        endpoint="", tenant_id="", client_id="", data_partition_id=""
    )
    with pytest.raises(ValueError, match="ADME connection is incomplete"):
        check_legal_tag(bad, token="t", legal_tag_name="x")


# ===========================================================================
# submit_manifest
# ===========================================================================


def test_submit_manifest_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-submit-1"},
            json_payload={
                "runId": "run-42",
                "workflowId": "wf-1",
                "status": "submitted",
            },
        ),
    )

    result = submit_manifest(_connection(), token="t", manifest_payload=_VALID_MANIFEST)

    assert isinstance(result, WorkflowRunResult)
    assert result.ok is True
    assert result.run_id == "run-42"
    assert result.workflow_id == "wf-1"
    assert result.raw_status == "submitted"
    assert result.status == WorkflowStatus.IN_PROGRESS
    assert result.correlation_id == "corr-submit-1"
    assert result.http_status == 200
    assert result.error_message is None
    assert captured[0]["url"].endswith(WORKFLOW_INGEST_RUN_PATH)
    # POST body MUST match the manifest payload exactly.
    assert captured[0]["json"] == _VALID_MANIFEST


def test_submit_manifest_2xx_without_run_id_surfaces_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "corr-norun"},
            json_payload={"status": "queued"},  # missing runId
        ),
    )

    result = submit_manifest(_connection(), token="t", manifest_payload=_VALID_MANIFEST)

    assert result.ok is False
    assert result.run_id is None
    assert result.error_message is not None
    assert "runId" in result.error_message
    assert result.status == WorkflowStatus.UNKNOWN
    assert result.correlation_id == "corr-norun"


@pytest.mark.parametrize("status_code", [400, 401, 403, 500])
def test_submit_manifest_http_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=status_code,
            json_payload={"message": f"boom {status_code}"},
        ),
    )

    result = submit_manifest(_connection(), token="t", manifest_payload=_VALID_MANIFEST)

    assert result.ok is False
    assert result.http_status == status_code
    assert result.run_id is None
    assert result.status == WorkflowStatus.UNKNOWN
    assert result.error_message is not None
    assert f"boom {status_code}" in result.error_message


def test_submit_manifest_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(ingestion_module.requests, "post", fake_post)

    result = submit_manifest(_connection(), token="t", manifest_payload=_VALID_MANIFEST)

    assert result.ok is False
    assert result.http_status is None
    assert result.error_message is not None
    assert "timed out" in result.error_message.lower()


def test_submit_manifest_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(**_: Any) -> Any:
        raise requests.exceptions.ConnectionError("dns")

    monkeypatch.setattr(ingestion_module.requests, "post", fake_post)

    result = submit_manifest(_connection(), token="t", manifest_payload=_VALID_MANIFEST)

    assert result.ok is False
    assert result.http_status is None
    assert "ConnectionError" in (result.error_message or "")


# ---------------------------------------------------------------------------
# put_records (Storage API PUT /records)
# ---------------------------------------------------------------------------


def _storage_record(record_id: str = "opendes:master-data--Well:1") -> dict[str, Any]:
    return {
        "id": record_id,
        "kind": "osdu:wks:master-data--Well:1.0.0",
        "acl": {"owners": ["o@x"], "viewers": ["v@x"]},
        "legal": {
            "legaltags": ["opendes-public-usa-dataset"],
            "otherRelevantDataCountries": ["US"],
        },
        "data": {"FacilityName": "W1"},
    }


def test_put_records_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [_storage_record("opendes:master-data--Well:1")]
    captured = _patch_put(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=201,
            headers={"correlation-id": "corr-put-1"},
            json_payload={
                "recordCount": 1,
                "recordIds": ["opendes:master-data--Well:1"],
                "skippedRecordIds": [],
            },
        ),
    )

    result = put_records(_connection(), token="t", records=records)

    assert result.ok is True
    assert result.http_status == 201
    assert result.record_count == 1
    assert result.record_ids == ["opendes:master-data--Well:1"]
    assert result.skipped_record_ids == []
    assert result.error_message is None
    assert result.correlation_id == "corr-put-1"
    # The PUT targets the Storage records endpoint with the records array.
    assert captured[0]["url"].endswith(STORAGE_RECORDS_PATH)
    assert captured[0]["json"] == records


def test_put_records_surfaces_skipped_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_put(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            json_payload={
                "recordCount": 1,
                "recordIds": ["opendes:master-data--Well:1"],
                "skippedRecordIds": ["opendes:master-data--Well:2"],
            },
        ),
    )

    result = put_records(
        _connection(),
        token="t",
        records=[_storage_record("opendes:master-data--Well:1")],
    )

    assert result.ok is True
    assert result.skipped_record_ids == ["opendes:master-data--Well:2"]


@pytest.mark.parametrize("status_code", [400, 401, 403, 409, 500])
def test_put_records_http_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _patch_put(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=status_code,
            json_payload={"message": f"boom {status_code}"},
        ),
    )

    result = put_records(
        _connection(), token="t", records=[_storage_record()]
    )

    assert result.ok is False
    assert result.http_status == status_code
    assert result.record_ids == []
    assert f"boom {status_code}" in (result.error_message or "")


def test_put_records_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="non-empty list"):
        put_records(_connection(), token="t", records=[])


def test_put_records_rejects_over_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        _storage_record(f"opendes:master-data--Well:{i}")
        for i in range(STORAGE_MAX_RECORDS_PER_CALL + 1)
    ]
    with pytest.raises(ValueError, match="at most"):
        put_records(_connection(), token="t", records=records)


def test_put_records_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_put(**_: Any) -> Any:
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(ingestion_module.requests, "put", fake_put)

    result = put_records(
        _connection(), token="t", records=[_storage_record()]
    )

    assert result.ok is False
    assert result.http_status is None
    assert "timed out" in (result.error_message or "").lower()


def test_submit_manifest_outgoing_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_post(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200, json_payload={"runId": "r1"}
        ),
    )

    submit_manifest(_connection(), token="bearer-abc", manifest_payload=_VALID_MANIFEST)

    headers = captured[0]["headers"]
    assert headers["Authorization"] == "Bearer bearer-abc"
    assert headers["data-partition-id"] == "example-opendes"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    assert captured[0]["timeout"] == INGESTION_TIMEOUT_SECONDS
    assert captured[0]["allow_redirects"] is False


@pytest.mark.parametrize("token", ["", "   "])
def test_submit_manifest_rejects_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="non-empty bearer token"):
        submit_manifest(
            _connection(), token=token, manifest_payload=_VALID_MANIFEST
        )


@pytest.mark.parametrize("payload", [{}, "not a dict", None])
def test_submit_manifest_rejects_invalid_payload(payload: Any) -> None:
    with pytest.raises(ValueError, match="manifest_payload"):
        submit_manifest(_connection(), token="t", manifest_payload=payload)


# ===========================================================================
# get_workflow_status
# ===========================================================================


@pytest.mark.parametrize(
    "raw_status, expected",
    [
        ("running", WorkflowStatus.IN_PROGRESS),
        ("Submitted", WorkflowStatus.IN_PROGRESS),
        ("queued", WorkflowStatus.IN_PROGRESS),
        ("finished", WorkflowStatus.FINISHED),
        ("Success", WorkflowStatus.FINISHED),
        ("succeeded", WorkflowStatus.FINISHED),
        ("completed", WorkflowStatus.FINISHED),
        ("failed", WorkflowStatus.FAILED),
        ("error", WorkflowStatus.FAILED),
        ("nonsense", WorkflowStatus.UNKNOWN),
        ("", WorkflowStatus.UNKNOWN),
    ],
)
def test_get_workflow_status_parses_each_documented_value(
    monkeypatch: pytest.MonkeyPatch,
    raw_status: str,
    expected: WorkflowStatus,
) -> None:
    _patch_get(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=200,
            headers={"correlation-id": "c"},
            json_payload={"runId": "r-1", "status": raw_status},
        ),
    )

    result = get_workflow_status(_connection(), token="t", run_id="r-1")

    assert result.ok is True
    assert result.raw_status == raw_status
    assert result.status == expected


def test_get_workflow_status_url_uses_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_get(
        monkeypatch,
        lambda **_: _FakeResponse(status_code=200, json_payload={"runId": "r-1"}),
    )

    get_workflow_status(_connection(), token="t", run_id="r-1")

    expected_path = WORKFLOW_RUN_STATUS_PATH_TEMPLATE.format(run_id="r-1")
    assert captured[0]["url"].endswith(expected_path)


@pytest.mark.parametrize("status_code", [401, 404, 500])
def test_get_workflow_status_http_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _patch_get(
        monkeypatch,
        lambda **_: _FakeResponse(
            status_code=status_code,
            json_payload={"message": f"boom {status_code}"},
        ),
    )

    result = get_workflow_status(_connection(), token="t", run_id="r-1")

    assert result.ok is False
    assert result.http_status == status_code
    assert result.status == WorkflowStatus.UNKNOWN
    assert result.error_message is not None
    assert f"boom {status_code}" in result.error_message


def test_get_workflow_status_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(**_: Any) -> Any:
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(ingestion_module.requests, "get", fake_get)

    result = get_workflow_status(_connection(), token="t", run_id="r-1")

    assert result.ok is False
    assert result.http_status is None
    assert "timed out" in (result.error_message or "").lower()


@pytest.mark.parametrize("run_id", ["", "   ", "\t"])
def test_get_workflow_status_rejects_blank_run_id(run_id: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        get_workflow_status(_connection(), token="t", run_id=run_id)


@pytest.mark.parametrize("token", ["", "   "])
def test_get_workflow_status_rejects_blank_token(token: str) -> None:
    with pytest.raises(ValueError, match="non-empty bearer token"):
        get_workflow_status(_connection(), token=token, run_id="r-1")
