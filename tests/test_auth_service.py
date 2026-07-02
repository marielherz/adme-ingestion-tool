"""Service-level tests for ADME authentication helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.connection import ADME_RESOURCE_SCOPE, ADMEConnection, AuthMethod
from app.services import auth as auth_module


def _user_connection(token_scope: str = ADME_RESOURCE_SCOPE) -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        token_scope=token_scope,
        auth_method=AuthMethod.USER_IMPERSONATION,
    )


def _service_principal_connection(
    token_scope: str = ADME_RESOURCE_SCOPE,
) -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        token_scope=token_scope,
        auth_method=AuthMethod.SERVICE_PRINCIPAL,
        client_secret="placeholder-client-secret",
    )


def test_user_auth_flow_uses_public_client_with_app_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakePublicClientApplication:
        def __init__(self, client_id: str, authority: str) -> None:
            captured["client_id"] = client_id
            captured["authority"] = authority

        def initiate_auth_code_flow(
            self,
            scopes: list[str],
            redirect_uri: str,
        ) -> dict[str, object]:
            captured["scopes"] = scopes
            captured["redirect_uri"] = redirect_uri
            return {
                "auth_uri": "https://login.example/authorize",
                "state": "placeholder-state",
                "code_verifier": "placeholder-verifier",
            }

    monkeypatch.setattr(
        auth_module.msal,
        "PublicClientApplication",
        FakePublicClientApplication,
    )

    custom_scope = "https://custom.example/.default"

    flow_start = auth_module.start_user_auth_flow(
        _user_connection(token_scope=custom_scope)
    )

    assert flow_start.authorization_url == "https://login.example/authorize"
    assert captured == {
        "client_id": "22222222-2222-2222-2222-222222222222",
        "authority": (
            "https://login.microsoftonline.com/"
            "11111111-1111-1111-1111-111111111111"
        ),
        "scopes": [custom_scope],
        "redirect_uri": auth_module.USER_AUTH_REDIRECT_URI,
    }
    assert not hasattr(auth_module, "InteractiveBrowserCredential")


def test_user_auth_state_supplies_token_without_opening_browser(
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
            assert auth_code_flow["state"] == "placeholder-state"
            assert auth_response == {
                "code": "placeholder-code",
                "state": "placeholder-state",
            }
            return {
                "access_token": "placeholder-user-token",
                "expires_in": "3600",
            }

    monkeypatch.setattr(
        auth_module.msal,
        "PublicClientApplication",
        FakePublicClientApplication,
    )
    monkeypatch.setattr(auth_module.time, "time", lambda: 10.0)

    user_auth_state = auth_module.complete_user_auth_flow(
        _user_connection(),
        {"state": "placeholder-state", "code_verifier": "placeholder-verifier"},
        {"code": "placeholder-code", "state": "placeholder-state"},
    )

    assert user_auth_state.expires_at == 3_610
    assert auth_module.get_token(_user_connection(), user_auth_state) == (
        "placeholder-user-token"
    )


def test_get_token_uses_client_secret_for_service_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClientSecretCredential:
        def __init__(
            self,
            tenant_id: str,
            client_id: str,
            client_secret: str,
        ) -> None:
            captured["tenant_id"] = tenant_id
            captured["client_id"] = client_id
            captured["client_secret"] = client_secret

        def get_token(self, scope: str) -> SimpleNamespace:
            captured["scope"] = scope
            return SimpleNamespace(token="placeholder-service-principal-token")

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(
        auth_module,
        "ClientSecretCredential",
        FakeClientSecretCredential,
    )

    custom_scope = "https://custom.example/.default"
    connection = _service_principal_connection(token_scope=custom_scope)

    assert auth_module.get_token(connection) == "placeholder-service-principal-token"
    assert captured == {
        "tenant_id": connection.tenant_id,
        "client_id": connection.client_id,
        "client_secret": "placeholder-client-secret",
        "scope": custom_scope,
        "closed": True,
    }


# ---------------------------------------------------------------------------
# Azure CLI token provider + refreshing wrapper
# ---------------------------------------------------------------------------


def _fake_completed(returncode: int, stdout: str = "", stderr: str = "") -> object:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_acquire_cli_token_returns_trimmed_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    monkeypatch.setattr(auth_module.shutil, "which", lambda _n: "C:/az.cmd")

    def fake_run(args: list[str], **kwargs: object) -> object:
        calls["args"] = args
        return _fake_completed(0, stdout="  the-token\n")

    monkeypatch.setattr(auth_module.subprocess, "run", fake_run)

    assert auth_module.acquire_cli_token() == "the-token"
    # Mirrors the manual command; no --resource by default.
    assert calls["args"][:4] == [
        "C:/az.cmd",
        "account",
        "get-access-token",
        "--query",
    ]
    assert "--resource" not in calls["args"]


def test_acquire_cli_token_passes_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_module.shutil, "which", lambda _n: "az")
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> object:
        captured["args"] = args
        return _fake_completed(0, stdout="tok")

    monkeypatch.setattr(auth_module.subprocess, "run", fake_run)

    auth_module.acquire_cli_token("api://the-app")
    assert captured["args"][-2:] == ["--resource", "api://the-app"]


def test_acquire_cli_token_missing_cli_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_module.shutil, "which", lambda _n: None)
    with pytest.raises(auth_module.AuthenticationError, match="was not found"):
        auth_module.acquire_cli_token()


def test_acquire_cli_token_nonzero_exit_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_module.shutil, "which", lambda _n: "az")
    monkeypatch.setattr(
        auth_module.subprocess,
        "run",
        lambda *a, **k: _fake_completed(1, stderr="Please run 'az login'"),
    )
    with pytest.raises(auth_module.AuthenticationError, match="az login"):
        auth_module.acquire_cli_token()


def test_acquire_cli_token_empty_output_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_module.shutil, "which", lambda _n: "az")
    monkeypatch.setattr(
        auth_module.subprocess, "run", lambda *a, **k: _fake_completed(0, stdout="  \n")
    )
    with pytest.raises(auth_module.AuthenticationError, match="empty token"):
        auth_module.acquire_cli_token()


def _jwt_with_exp(exp: int) -> str:
    import base64
    import json

    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.sig"


def test_refreshing_token_provider_caches_until_near_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000_000
    monkeypatch.setattr(auth_module.time, "time", lambda: now)

    minted: list[str] = []

    def acquire() -> str:
        token = _jwt_with_exp(now + 3600)  # expires in 1h
        minted.append(token)
        return token

    provider = auth_module.RefreshingTokenProvider(acquire, skew_seconds=300)

    first = provider()
    second = provider()
    assert first == second
    assert len(minted) == 1  # cached — not re-acquired while far from expiry


def test_refreshing_token_provider_refreshes_when_expiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1_000_000}
    monkeypatch.setattr(auth_module.time, "time", lambda: clock["now"])

    count = {"n": 0}

    def acquire() -> str:
        count["n"] += 1
        # each token expires 100s out — always within the 300s skew.
        return _jwt_with_exp(clock["now"] + 100)

    provider = auth_module.RefreshingTokenProvider(acquire, skew_seconds=300)
    provider()
    provider()
    assert count["n"] == 2  # re-acquired because within the skew window

