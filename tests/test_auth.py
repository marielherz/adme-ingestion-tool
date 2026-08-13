"""Tests for ADME authentication helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.identity import CredentialUnavailableError

from app.models.connection import ADME_RESOURCE_SCOPE, ADMEConnection, AuthMethod
from app.services import auth as auth_module
from app.services.auth import (
    AuthenticationError,
    UserAuthFlowStart,
    UserAuthState,
    complete_user_auth_flow,
    get_token,
    start_user_auth_flow,
    user_auth_state_from_pasted_token,
)


def _user_impersonation_connection(
    token_scope: str = ADME_RESOURCE_SCOPE,
) -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="tenant-id",
        client_id="client-id",
        data_partition_id="example-opendes",
        token_scope=token_scope,
        auth_method=AuthMethod.USER_IMPERSONATION,
    )


def _service_principal_connection(
    token_scope: str = ADME_RESOURCE_SCOPE,
) -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="tenant-id",
        client_id="client-id",
        data_partition_id="example-opendes",
        token_scope=token_scope,
        auth_method=AuthMethod.SERVICE_PRINCIPAL,
        client_secret="placeholder-client-secret",
    )


def test_start_user_auth_flow_uses_msal_public_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakePublicClientApplication:
        def __init__(self, client_id: str, authority: str) -> None:
            calls["client_id"] = client_id
            calls["authority"] = authority

        def initiate_auth_code_flow(
            self,
            scopes: list[str],
            redirect_uri: str,
        ) -> dict[str, object]:
            calls["scopes"] = scopes
            calls["redirect_uri"] = redirect_uri
            return {
                "auth_uri": "https://login.example/authorize?state=opaque-state",
                "state": "opaque-state",
                "code_verifier": "opaque-verifier",
            }

    monkeypatch.setattr(
        auth_module.msal,
        "PublicClientApplication",
        FakePublicClientApplication,
    )

    custom_scope = "https://custom.example/.default"

    flow_start = start_user_auth_flow(
        _user_impersonation_connection(token_scope=custom_scope)
    )

    assert flow_start.authorization_url == (
        "https://login.example/authorize?state=opaque-state"
    )
    assert flow_start.auth_url == flow_start.authorization_url
    assert flow_start.flow["state"] == "opaque-state"
    assert calls == {
        "client_id": "client-id",
        "authority": "https://login.microsoftonline.com/tenant-id",
        "scopes": [custom_scope],
        "redirect_uri": "http://localhost:8501",
    }
    assert "opaque-state" not in repr(flow_start)
    assert "opaque-verifier" not in repr(flow_start)
    assert not hasattr(auth_module, "InteractiveBrowserCredential")


def test_complete_user_auth_flow_exchanges_callback_and_returns_session_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakePublicClientApplication:
        def __init__(self, client_id: str, authority: str) -> None:
            calls["client_id"] = client_id
            calls["authority"] = authority

        def acquire_token_by_auth_code_flow(
            self,
            auth_code_flow: dict[str, object],
            auth_response: dict[str, str],
        ) -> dict[str, object]:
            calls["auth_code_flow"] = auth_code_flow
            calls["auth_response"] = auth_response
            return {
                "access_token": "placeholder-user-token",
                "expires_in": 3600,
            }

    monkeypatch.setattr(
        auth_module.msal,
        "PublicClientApplication",
        FakePublicClientApplication,
    )
    monkeypatch.setattr(auth_module.time, "time", lambda: 1_000.0)

    state = complete_user_auth_flow(
        _user_impersonation_connection(),
        {"state": "opaque-state", "code_verifier": "opaque-verifier"},
        {"code": ["auth-code"], "state": "opaque-state"},
    )

    assert state.access_token == "placeholder-user-token"
    assert state.expires_at == 4_600
    assert get_token(_user_impersonation_connection(), state) == (
        "placeholder-user-token"
    )
    assert calls == {
        "client_id": "client-id",
        "authority": "https://login.microsoftonline.com/tenant-id",
        "auth_code_flow": {
            "state": "opaque-state",
            "code_verifier": "opaque-verifier",
        },
        "auth_response": {"code": "auth-code", "state": "opaque-state"},
    }
    assert "placeholder-user-token" not in repr(state)


def test_complete_user_auth_flow_accepts_flow_start_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePublicClientApplication:
        def __init__(self, client_id: str, authority: str) -> None:
            del client_id, authority

        def acquire_token_by_auth_code_flow(
            self,
            auth_code_flow: dict[str, object],
            auth_response: dict[str, str],
        ) -> dict[str, object]:
            assert auth_code_flow["state"] == "opaque-state"
            assert auth_response["code"] == "auth-code"
            return {"access_token": "placeholder-user-token"}

    monkeypatch.setattr(
        auth_module.msal,
        "PublicClientApplication",
        FakePublicClientApplication,
    )

    flow_start = UserAuthFlowStart(
        authorization_url="https://login.example/authorize",
        flow={"state": "opaque-state", "code_verifier": "opaque-verifier"},
    )

    state = complete_user_auth_flow(
        _user_impersonation_connection(),
        flow_start,
        {"code": "auth-code", "state": "opaque-state"},
    )

    assert state.access_token == "placeholder-user-token"


def test_get_token_requires_completed_user_auth_state() -> None:
    with pytest.raises(AuthenticationError, match="User sign-in is required"):
        get_token(_user_impersonation_connection())


def test_get_token_rejects_expired_user_auth_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_module.time, "time", lambda: 1_000.0)

    with pytest.raises(AuthenticationError, match="User sign-in has expired"):
        get_token(
            _user_impersonation_connection(),
            UserAuthState(
                access_token="placeholder-user-token",
                expires_at=1_059,
            ),
        )


def test_get_token_uses_client_secret_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeCredential:
        def __init__(self, **kwargs: object) -> None:
            calls["kwargs"] = kwargs

        def get_token(self, scope: str) -> SimpleNamespace:
            calls["scope"] = scope
            return SimpleNamespace(token="placeholder-service-principal-token")

        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr("app.services.auth.ClientSecretCredential", FakeCredential)

    custom_scope = "https://custom.example/.default"

    token = get_token(_service_principal_connection(token_scope=custom_scope))

    assert token == "placeholder-service-principal-token"
    assert calls["scope"] == custom_scope
    assert calls["kwargs"] == {
        "tenant_id": "tenant-id",
        "client_id": "client-id",
        "client_secret": "placeholder-client-secret",
    }
    assert calls["closed"] is True


def test_get_token_rejects_invalid_connection() -> None:
    with pytest.raises(ValueError, match="ADME connection is incomplete"):
        get_token(
            ADMEConnection(
                endpoint="https://example.energy.azure.com",
                tenant_id="tenant-id",
                client_id="client-id",
                data_partition_id="",
            )
        )


@pytest.mark.parametrize(
    ("exception", "expected_message"),
    [
        pytest.param(
            CredentialUnavailableError(message="credential unavailable"),
            "service_principal credential is unavailable",
            id="credential-unavailable",
        ),
        pytest.param(
            ClientAuthenticationError(message="authentication failed"),
            "service_principal authentication failed",
            id="client-authentication-error",
        ),
        pytest.param(
            AzureError("token failed"),
            "service_principal token acquisition failed",
            id="azure-error",
        ),
    ],
)
def test_service_principal_auth_errors_use_existing_error_pattern(
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
    expected_message: str,
) -> None:
    calls: dict[str, object] = {}

    class FakeCredential:
        def __init__(self, **kwargs: object) -> None:
            calls["kwargs"] = kwargs

        def get_token(self, scope: str) -> SimpleNamespace:
            calls["scope"] = scope
            raise exception

        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr("app.services.auth.ClientSecretCredential", FakeCredential)

    with pytest.raises(AuthenticationError, match=expected_message):
        get_token(_service_principal_connection())

    assert calls["scope"] == ADME_RESOURCE_SCOPE
    assert calls["closed"] is True


def test_start_user_auth_flow_sanitizes_msal_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePublicClientApplication:
        def __init__(self, client_id: str, authority: str) -> None:
            del client_id, authority

        def initiate_auth_code_flow(
            self,
            scopes: list[str],
            redirect_uri: str,
        ) -> dict[str, object]:
            del scopes, redirect_uri
            return {
                "error": "invalid_client",
                "error_description": "raw-msal-description",
            }

    monkeypatch.setattr(
        auth_module.msal,
        "PublicClientApplication",
        FakePublicClientApplication,
    )

    with pytest.raises(AuthenticationError) as exc_info:
        start_user_auth_flow(_user_impersonation_connection())

    message = str(exc_info.value)
    assert "invalid_client" in message
    assert "raw-msal-description" not in message


def test_complete_user_auth_flow_sanitizes_callback_errors() -> None:
    with pytest.raises(AuthenticationError) as exc_info:
        complete_user_auth_flow(
            _user_impersonation_connection(),
            {"state": "opaque-state"},
            {
                "error": "access_denied",
                "error_description": "raw-callback-description",
            },
        )

    message = str(exc_info.value)
    assert "access_denied" in message
    assert "raw-callback-description" not in message
    assert "opaque-state" not in message


def test_complete_user_auth_flow_sanitizes_state_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePublicClientApplication:
        def __init__(self, client_id: str, authority: str) -> None:
            del client_id, authority

        def acquire_token_by_auth_code_flow(
            self,
            auth_code_flow: dict[str, object],
            auth_response: dict[str, str],
        ) -> dict[str, object]:
            del auth_code_flow, auth_response
            raise ValueError("state mismatch for opaque-verifier")

    monkeypatch.setattr(
        auth_module.msal,
        "PublicClientApplication",
        FakePublicClientApplication,
    )

    with pytest.raises(AuthenticationError) as exc_info:
        complete_user_auth_flow(
            _user_impersonation_connection(),
            {"state": "opaque-state", "code_verifier": "opaque-verifier"},
            {"code": "auth-code", "state": "wrong-state"},
        )

    message = str(exc_info.value)
    assert "pending authentication flow" in message
    assert "opaque-verifier" not in message
    assert "wrong-state" not in message


def test_complete_user_auth_flow_sanitizes_msal_result_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePublicClientApplication:
        def __init__(self, client_id: str, authority: str) -> None:
            del client_id, authority

        def acquire_token_by_auth_code_flow(
            self,
            auth_code_flow: dict[str, object],
            auth_response: dict[str, str],
        ) -> dict[str, object]:
            del auth_code_flow, auth_response
            return {
                "error": "invalid_grant",
                "error_description": "raw-msal-result-description",
            }

    monkeypatch.setattr(
        auth_module.msal,
        "PublicClientApplication",
        FakePublicClientApplication,
    )

    with pytest.raises(AuthenticationError) as exc_info:
        complete_user_auth_flow(
            _user_impersonation_connection(),
            {"state": "opaque-state", "code_verifier": "opaque-verifier"},
            {"code": "auth-code", "state": "opaque-state"},
        )

    message = str(exc_info.value)
    assert "invalid_grant" in message
    assert "raw-msal-result-description" not in message


def test_get_token_rejects_unsupported_auth_method() -> None:
    connection = ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="tenant-id",
        client_id="client-id",
        data_partition_id="example-opendes",
        auth_method="unsupported",  # type: ignore[arg-type]
    )

    with pytest.raises(
        AuthenticationError,
        match="Unsupported authentication method: 'unsupported'.",
    ):
        get_token(connection)


# ---------------------------------------------------------------------------
# user_auth_state_from_pasted_token
# ---------------------------------------------------------------------------


def _make_jwt(payload: dict[str, object]) -> str:
    """Return a JWT-looking string with the given payload claim set."""
    import base64
    import json

    def seg(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = seg(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
    body = seg(json.dumps(payload).encode("utf-8"))
    return f"{header}.{body}.{seg(b'sig')}"


def test_user_auth_state_from_pasted_token_accepts_valid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_module.time, "time", lambda: 1_000.0)
    token = _make_jwt({"oid": "abc", "exp": 5_000})

    state = user_auth_state_from_pasted_token(f"  {token}  ")

    assert state.access_token == token
    assert state.expires_at == 5_000
    assert not state.is_expired()


def test_user_auth_state_from_pasted_token_rejects_empty() -> None:
    with pytest.raises(AuthenticationError, match="non-empty"):
        user_auth_state_from_pasted_token("   ")


def test_user_auth_state_from_pasted_token_rejects_non_jwt() -> None:
    with pytest.raises(AuthenticationError, match="three"):
        user_auth_state_from_pasted_token("not-a-jwt")


def test_user_auth_state_from_pasted_token_rejects_token_without_exp() -> None:
    token = _make_jwt({"oid": "abc"})

    with pytest.raises(AuthenticationError, match="expiry"):
        user_auth_state_from_pasted_token(token)


def test_user_auth_state_from_pasted_token_rejects_expired_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_module.time, "time", lambda: 10_000.0)
    token = _make_jwt({"oid": "abc", "exp": 5_000})

    with pytest.raises(AuthenticationError, match="already expired"):
        user_auth_state_from_pasted_token(token)
