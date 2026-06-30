"""Tests for session-scoped ADME connection state helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.connection_state import (
    CONNECTION_KEY,
    DEFAULT_CONNECTION_NAME,
    HEALTH_ERROR_KEY,
    HEALTH_RESULTS_KEY,
    USER_AUTH_FLOW_KEY,
    USER_AUTH_STATE_KEY,
    AuthReadiness,
    auth_readiness,
    clear_pending_user_auth_flow,
    clear_user_auth_state,
    ensure_session_defaults,
    format_auth_method,
    get_overall_state,
    get_pending_user_auth_flow,
    get_user_auth_state,
    results_to_table_rows,
    save_connection,
    store_pending_user_auth_flow,
    store_user_auth_state,
    summarize_health,
)
from app.models.connection import ADMEConnection, AuthMethod, ServiceHealthResult
from app.services import settings_store
from app.services.auth import UserAuthFlowStart, UserAuthState


@pytest.fixture
def isolated_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point the on-disk settings store at tmp_path for the duration of the test."""
    target = tmp_path / "settings.db"
    monkeypatch.setenv("ADME_SETTINGS_DB", str(target))
    return target


def test_get_overall_state_requires_a_valid_connection() -> None:
    session_state: dict[str, object] = {}
    assert get_overall_state(session_state) == "not_configured"

    session_state[CONNECTION_KEY] = ADMEConnection(
        endpoint="",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
    )
    assert get_overall_state(session_state) == "not_configured"


def test_ensure_session_defaults_initializes_user_auth_keys() -> None:
    session_state: dict[str, object] = {}

    ensure_session_defaults(session_state)

    assert session_state[USER_AUTH_FLOW_KEY] is None
    assert session_state[USER_AUTH_STATE_KEY] is None


def test_get_overall_state_prioritizes_latest_error() -> None:
    session_state: dict[str, object] = {
        CONNECTION_KEY: ADMEConnection(
            endpoint="https://example.energy.azure.com",
            tenant_id="11111111-1111-1111-1111-111111111111",
            client_id="22222222-2222-2222-2222-222222222222",
            data_partition_id="example-opendes",
        ),
        HEALTH_RESULTS_KEY: [
            ServiceHealthResult(
                service_name="Storage",
                path="/api/storage/v2/query/kinds?limit=1",
                status="healthy",
                status_code=200,
                response_time_ms=15.0,
            )
        ],
        HEALTH_ERROR_KEY: "Timed out.",
    }

    assert get_overall_state(session_state) == "error"


def test_store_and_clear_pending_user_auth_flow() -> None:
    session_state: dict[str, object] = {}
    flow_start = UserAuthFlowStart(
        authorization_url="https://login.example.test/authorize",
        flow={"state": "placeholder-state"},
    )

    store_pending_user_auth_flow(session_state, flow_start)
    assert get_pending_user_auth_flow(session_state) == flow_start

    clear_pending_user_auth_flow(session_state)
    assert get_pending_user_auth_flow(session_state) is None


def test_store_user_auth_state_clears_stale_health() -> None:
    session_state: dict[str, object] = {
        HEALTH_RESULTS_KEY: [
            ServiceHealthResult(
                service_name="Storage",
                path="/storage",
                status="healthy",
                status_code=200,
            )
        ],
        HEALTH_ERROR_KEY: "Old validation error.",
    }
    auth_state = UserAuthState(access_token="placeholder-user-token")

    store_user_auth_state(session_state, auth_state)

    assert get_user_auth_state(session_state) == auth_state
    assert session_state[HEALTH_RESULTS_KEY] == []
    assert session_state[HEALTH_ERROR_KEY] == ""


def test_clear_user_auth_state_clears_pending_flow_and_health() -> None:
    session_state: dict[str, object] = {
        USER_AUTH_FLOW_KEY: UserAuthFlowStart(
            authorization_url="https://login.example.test/authorize",
            flow={"state": "placeholder-state"},
        ),
        USER_AUTH_STATE_KEY: UserAuthState(access_token="placeholder-user-token"),
        HEALTH_RESULTS_KEY: [
            ServiceHealthResult(
                service_name="Storage",
                path="/storage",
                status="healthy",
                status_code=200,
            )
        ],
        HEALTH_ERROR_KEY: "",
    }

    clear_user_auth_state(session_state)

    assert session_state[USER_AUTH_FLOW_KEY] is None
    assert session_state[USER_AUTH_STATE_KEY] is None
    assert session_state[HEALTH_RESULTS_KEY] == []
    assert session_state[HEALTH_ERROR_KEY] == ""


def test_save_connection_clears_user_auth_when_connection_changes() -> None:
    session_state: dict[str, object] = {
        CONNECTION_KEY: ADMEConnection(
            endpoint="https://old.energy.azure.com",
            tenant_id="11111111-1111-1111-1111-111111111111",
            client_id="22222222-2222-2222-2222-222222222222",
            data_partition_id="old-opendes",
        ),
        USER_AUTH_STATE_KEY: UserAuthState(access_token="placeholder-user-token"),
        HEALTH_RESULTS_KEY: [
            ServiceHealthResult(
                service_name="Storage",
                path="/storage",
                status="healthy",
                status_code=200,
            )
        ],
        HEALTH_ERROR_KEY: "",
    }

    new_connection = ADMEConnection(
        endpoint="https://new.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="new-opendes",
    )
    save_connection(session_state, new_connection)

    assert session_state[CONNECTION_KEY] == new_connection
    assert session_state[USER_AUTH_STATE_KEY] is None
    assert session_state[HEALTH_RESULTS_KEY] == []


def test_save_connection_clears_user_auth_when_token_scope_changes() -> None:
    session_state: dict[str, object] = {
        CONNECTION_KEY: ADMEConnection(
            endpoint="https://example.energy.azure.com",
            tenant_id="11111111-1111-1111-1111-111111111111",
            client_id="22222222-2222-2222-2222-222222222222",
            data_partition_id="example-opendes",
            token_scope="https://old.energy.azure.com/.default",
        ),
        USER_AUTH_FLOW_KEY: UserAuthFlowStart(
            authorization_url="https://login.example.test/authorize",
            flow={"state": "placeholder-state"},
        ),
        USER_AUTH_STATE_KEY: UserAuthState(access_token="placeholder-user-token"),
        HEALTH_RESULTS_KEY: [
            ServiceHealthResult(
                service_name="Storage",
                path="/storage",
                status="healthy",
                status_code=200,
            )
        ],
        HEALTH_ERROR_KEY: "Old validation error.",
    }

    new_connection = ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        token_scope="https://new.energy.azure.com/.default",
    )
    save_connection(session_state, new_connection)

    assert session_state[CONNECTION_KEY] == new_connection
    assert session_state[USER_AUTH_FLOW_KEY] is None
    assert session_state[USER_AUTH_STATE_KEY] is None
    assert session_state[HEALTH_RESULTS_KEY] == []
    assert session_state[HEALTH_ERROR_KEY] == ""


def test_save_connection_preserves_session_when_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_connection = ADMEConnection(
        endpoint="https://old.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="old-opendes",
    )
    auth_state = UserAuthState(access_token="placeholder-user-token")
    pending_flow = UserAuthFlowStart(
        authorization_url="https://login.example.test/authorize",
        flow={"state": "placeholder-state"},
    )
    health_results = [
        ServiceHealthResult(
            service_name="Storage",
            path="/storage",
            status="healthy",
            status_code=200,
        )
    ]
    session_state: dict[str, object] = {
        CONNECTION_KEY: old_connection,
        USER_AUTH_FLOW_KEY: pending_flow,
        USER_AUTH_STATE_KEY: auth_state,
        HEALTH_RESULTS_KEY: health_results,
        HEALTH_ERROR_KEY: "",
    }
    new_connection = ADMEConnection(
        endpoint="https://new.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="new-opendes",
    )

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise settings_store.SettingsStoreError("keyring locked")

    def fail_set_active(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("set_active_connection should not be called")

    monkeypatch.setattr(settings_store, "save_connection", fail_save)
    monkeypatch.setattr(settings_store, "set_active_connection", fail_set_active)

    with pytest.raises(settings_store.SettingsStoreError, match="keyring locked"):
        save_connection(session_state, new_connection)

    assert session_state[CONNECTION_KEY] == old_connection
    assert session_state[USER_AUTH_FLOW_KEY] == pending_flow
    assert session_state[USER_AUTH_STATE_KEY] == auth_state
    assert session_state[HEALTH_RESULTS_KEY] == health_results
    assert session_state[HEALTH_ERROR_KEY] == ""


def test_summarize_health_counts_each_state() -> None:
    results = [
        ServiceHealthResult(
            service_name="Storage",
            path="/storage",
            status="healthy",
            status_code=200,
            response_time_ms=12.0,
        ),
        ServiceHealthResult(
            service_name="Search",
            path="/search",
            status="unhealthy",
            status_code=403,
            response_time_ms=18.0,
            error_message="Forbidden",
        ),
        ServiceHealthResult(
            service_name="Workflow",
            path="/workflow",
            status="error",
            response_time_ms=5000.0,
            error_message="Timed out",
        ),
    ]

    summary = summarize_health(results)
    assert summary.total_services == 3
    assert summary.healthy_services == 1
    assert summary.unhealthy_services == 1
    assert summary.error_services == 1
    assert summary.overall_state == "error"


def test_results_to_table_rows_are_operator_friendly() -> None:
    rows = results_to_table_rows(
        [
            ServiceHealthResult(
                service_name="EDS",
                path="/api/eds/v1/retrievalInstructions",
                status="unhealthy",
                status_code=500,
                response_time_ms=11.234,
                error_message="Internal error",
            )
        ]
    )

    assert rows == [
        {
            "Service": "EDS",
            "State": "⚠️ Unhealthy",
            "HTTP": 500,
            "Latency (ms)": 11.2,
            "Detail": "Internal error",
        }
    ]
    assert format_auth_method(AuthMethod.SERVICE_PRINCIPAL) == "Service principal"


def test_ensure_session_defaults_hydrates_from_active_stored_connection(
    isolated_store: Path,
) -> None:
    stored = ADMEConnection(
        endpoint="https://stored.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="stored-opendes",
    )
    settings_store.save_connection(DEFAULT_CONNECTION_NAME, stored)
    settings_store.set_active_connection(DEFAULT_CONNECTION_NAME)

    session_state: dict[str, object] = {}
    ensure_session_defaults(session_state)

    hydrated = session_state[CONNECTION_KEY]
    assert isinstance(hydrated, ADMEConnection)
    assert hydrated.endpoint == stored.endpoint
    assert hydrated.data_partition_id == stored.data_partition_id
    # Stored connection had no client_secret, so the keyring lookup misses
    # and the field hydrates as the empty string.
    assert hydrated.client_secret == ""


def test_ensure_session_defaults_no_hydration_when_nothing_active(
    isolated_store: Path,
) -> None:
    session_state: dict[str, object] = {}

    ensure_session_defaults(session_state)

    assert session_state[CONNECTION_KEY] is None


def _jwt_with(payload: dict[str, object]) -> str:
    import base64
    import json

    def _seg(data: dict[str, object]) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_seg({'alg': 'none'})}.{_seg(payload)}.signature"


def _connection(
    auth_method: AuthMethod = AuthMethod.USER_IMPERSONATION,
    client_secret: str = "",
) -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        auth_method=auth_method,
        client_secret=client_secret,
    )


def test_auth_readiness_none_connection_not_ready() -> None:
    result = auth_readiness(None, None)

    assert isinstance(result, AuthReadiness)
    assert result.ready is False
    assert result.identity_label is None
    assert "Sign in or paste" in result.guidance


def test_auth_readiness_service_principal_ready_with_secret() -> None:
    connection = _connection(AuthMethod.SERVICE_PRINCIPAL, client_secret="s3cret")

    result = auth_readiness(connection, None)

    assert result.ready is True
    assert result.method == AuthMethod.SERVICE_PRINCIPAL
    assert result.identity_label is not None
    assert "service principal" in result.identity_label


def test_auth_readiness_service_principal_missing_secret_not_ready() -> None:
    connection = _connection(AuthMethod.SERVICE_PRINCIPAL, client_secret="  ")

    result = auth_readiness(connection, None)

    assert result.ready is False
    assert "client secret" in result.guidance.lower()


def test_auth_readiness_user_impersonation_ready_with_token() -> None:
    connection = _connection(AuthMethod.USER_IMPERSONATION)
    token = _jwt_with({"upn": "mariel@example.com", "exp": 9999999999})
    state = UserAuthState(access_token=token, expires_at=9999999999)

    result = auth_readiness(connection, state)

    assert result.ready is True
    assert result.identity_label == "mariel@example.com"


def test_auth_readiness_user_impersonation_no_token_not_ready() -> None:
    connection = _connection(AuthMethod.USER_IMPERSONATION)

    result = auth_readiness(connection, None)

    assert result.ready is False
    assert "Sign in or paste" in result.guidance


def test_auth_readiness_user_impersonation_expired_token_guidance() -> None:
    connection = _connection(AuthMethod.USER_IMPERSONATION)
    state = UserAuthState(access_token="placeholder", expires_at=0)

    result = auth_readiness(connection, state)

    assert result.ready is False
    assert "expired" in result.guidance.lower()


def test_auth_readiness_user_impersonation_falls_back_to_generic_label() -> None:
    connection = _connection(AuthMethod.USER_IMPERSONATION)
    state = UserAuthState(access_token="not-a-jwt", expires_at=9999999999)

    result = auth_readiness(connection, state)

    assert result.ready is True
    assert result.identity_label == "signed-in user"


def test_ensure_session_defaults_preserves_existing_session_connection(
    isolated_store: Path,
) -> None:
    on_disk = ADMEConnection(
        endpoint="https://disk.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="disk-opendes",
    )
    settings_store.save_connection(DEFAULT_CONNECTION_NAME, on_disk)
    settings_store.set_active_connection(DEFAULT_CONNECTION_NAME)

    in_session = ADMEConnection(
        endpoint="https://session.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="session-opendes",
    )
    session_state: dict[str, object] = {CONNECTION_KEY: in_session}

    ensure_session_defaults(session_state)

    # Session value wins; disk value must not overwrite an in-flight edit.
    assert session_state[CONNECTION_KEY] is in_session


def test_ensure_session_defaults_swallows_store_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise settings_store.SettingsStoreError("disk on fire")

    monkeypatch.setattr(settings_store, "initialize_store", _boom)

    session_state: dict[str, object] = {}
    # Must not raise — hydration is best-effort.
    ensure_session_defaults(session_state)

    assert session_state[CONNECTION_KEY] is None
    assert session_state[HEALTH_RESULTS_KEY] == []
    assert session_state[HEALTH_ERROR_KEY] == ""
    assert session_state[USER_AUTH_FLOW_KEY] is None
    assert session_state[USER_AUTH_STATE_KEY] is None


def test_store_user_auth_state_persists_token(isolated_store: Path) -> None:
    settings_store.initialize_store()
    settings_store.save_connection(DEFAULT_CONNECTION_NAME, _connection())
    settings_store.set_active_connection(DEFAULT_CONNECTION_NAME)
    session_state: dict[str, object] = {
        USER_AUTH_STATE_KEY: None,
        HEALTH_RESULTS_KEY: [],
        HEALTH_ERROR_KEY: "",
    }

    store_user_auth_state(
        session_state,
        UserAuthState(access_token="aa.bb.cc", expires_at=1700000000),
    )

    assert settings_store.load_user_token() == ("aa.bb.cc", 1700000000)


def test_clear_user_auth_state_forgets_persisted_token(
    isolated_store: Path,
) -> None:
    settings_store.initialize_store()
    settings_store.save_connection(DEFAULT_CONNECTION_NAME, _connection())
    settings_store.set_active_connection(DEFAULT_CONNECTION_NAME)
    settings_store.save_user_token(
        "aa.bb.cc", 1700000000, name=DEFAULT_CONNECTION_NAME
    )
    session_state: dict[str, object] = {
        USER_AUTH_STATE_KEY: "placeholder",
        USER_AUTH_FLOW_KEY: None,
        HEALTH_RESULTS_KEY: [],
        HEALTH_ERROR_KEY: "",
    }

    clear_user_auth_state(session_state)

    assert settings_store.load_user_token() is None
    assert session_state[USER_AUTH_STATE_KEY] is None


def test_ensure_session_defaults_hydrates_persisted_token(
    isolated_store: Path,
) -> None:
    settings_store.initialize_store()
    settings_store.save_connection(DEFAULT_CONNECTION_NAME, _connection())
    settings_store.set_active_connection(DEFAULT_CONNECTION_NAME)
    settings_store.save_user_token(
        "aa.bb.cc", 1700000000, name=DEFAULT_CONNECTION_NAME
    )
    session_state: dict[str, object] = {}

    ensure_session_defaults(session_state)

    restored = session_state[USER_AUTH_STATE_KEY]
    assert restored is not None
    assert restored.access_token == "aa.bb.cc"
    assert restored.expires_at == 1700000000


def test_save_connection_persists_to_store_and_marks_active(
    isolated_store: Path,
) -> None:
    session_state: dict[str, object] = {}
    ensure_session_defaults(session_state)

    new_connection = ADMEConnection(
        endpoint="https://new.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="new-opendes",
        auth_method=AuthMethod.SERVICE_PRINCIPAL,
        client_secret="never-persisted",
    )
    save_connection(session_state, new_connection)

    assert (
        settings_store.get_active_connection_name() == DEFAULT_CONNECTION_NAME
    )
    persisted = settings_store.load_connection(DEFAULT_CONNECTION_NAME)
    assert persisted is not None
    assert persisted.endpoint == new_connection.endpoint
    assert persisted.auth_method == AuthMethod.SERVICE_PRINCIPAL
    # Secret is now persisted in the OS keyring (faked in tests),
    # not in the SQLite file.
    assert persisted.client_secret == "never-persisted"
