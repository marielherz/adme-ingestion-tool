"""Tests for the ADME settings page flow."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.connection_state import (
    CONNECTION_KEY,
    HEALTH_ERROR_KEY,
    HEALTH_RESULTS_KEY,
    USER_AUTH_FLOW_KEY,
    USER_AUTH_STATE_KEY,
)
from app.models.connection import (
    ADME_RESOURCE_SCOPE,
    ADMEConnection,
    AuthMethod,
    ServiceHealthResult,
)
from app.services import settings_store
from app.services.auth import AuthenticationError, UserAuthFlowStart, UserAuthState
from app.storage_bridge import StorageSyncStatus
from tests.support.streamlit_recorder import StreamlitRecorder

SETTINGS_PAGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "pages"
    / "1_⚙️_Instance_Configuration.py"
)


def _load_settings_module(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    monkeypatch.setitem(sys.modules, "streamlit", streamlit_recorder)
    module_name = "tests.generated_settings_page"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, SETTINGS_PAGE_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "load_persisted_connection_state",
        lambda session_state: StorageSyncStatus(available=True),
    )
    monkeypatch.setattr(
        module,
        "persist_connection_profile",
        lambda connection: StorageSyncStatus(available=True),
    )
    monkeypatch.setattr(
        module,
        "persist_health_run",
        lambda connection, results: StorageSyncStatus(available=True),
    )
    return module


def _user_connection(
    token_scope: str = ADME_RESOURCE_SCOPE,
) -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        token_scope=token_scope,
    )


def _auth_state() -> UserAuthState:
    return UserAuthState(access_token="placeholder-user-token")


def _flow_start() -> UserAuthFlowStart:
    return UserAuthFlowStart(
        authorization_url="https://login.example.test/authorize",
        flow={"state": "placeholder-state"},
    )


def _named_flow_start(name: str) -> UserAuthFlowStart:
    return UserAuthFlowStart(
        authorization_url=f"https://login.example.test/{name}/authorize",
        flow={"state": f"{name}-state"},
    )


def test_settings_page_renders_client_secret_for_service_principal(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.widget_values["Authentication method"] = (
        AuthMethod.SERVICE_PRINCIPAL
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    client_secret_calls = [
        call
        for call in streamlit_recorder.calls_named("text_input")
        if call.args == ("Client secret",)
    ]
    assert client_secret_calls
    assert client_secret_calls[0].kwargs["type"] == "password"
    captions = [call.args[0] for call in streamlit_recorder.calls_named("caption")]
    assert any("OS credential store" in message for message in captions)
    assert all("stored only for this session" not in message for message in captions)


def test_settings_page_hides_client_secret_for_user_impersonation(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.widget_values["Authentication method"] = (
        AuthMethod.USER_IMPERSONATION
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    client_secret_calls = [
        call
        for call in streamlit_recorder.calls_named("text_input")
        if call.args == ("Client secret",)
    ]
    assert client_secret_calls == []
    info_messages = [call.args[0] for call in streamlit_recorder.calls_named("info")]
    assert any(
        "Save your connection above" in message for message in info_messages
    )
    assert all("device-code" not in message.lower() for message in info_messages)
    assert all("enter code" not in message.lower() for message in info_messages)
    assert all(
        "separate browser tab" not in message.lower() for message in info_messages
    )


def test_settings_page_keeps_issue_field_contract_without_extra_inputs(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    text_input_labels = {
        call.args[0] for call in streamlit_recorder.calls_named("text_input")
    }
    assert text_input_labels == {
        "ADME endpoint",
        "Tenant ID",
        "Client ID",
        "Token scope",
        "Data partition ID",
    }
    [radio_call] = streamlit_recorder.calls_named("radio")
    assert radio_call.args[0] == "Authentication method"


def test_settings_page_defaults_token_scope_to_adme_resource_scope(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    [token_scope_call] = [
        call
        for call in streamlit_recorder.calls_named("text_input")
        if call.args == ("Token scope",)
    ]
    assert token_scope_call.kwargs["key"] == "cfg_token_scope"
    assert streamlit_recorder.session_state["cfg_token_scope"] == ADME_RESOURCE_SCOPE
    assert token_scope_call.kwargs["placeholder"] == ADME_RESOURCE_SCOPE
    help_text = token_scope_call.kwargs["help"]
    assert "OAuth scope" in help_text
    assert "ADME resource scope" in help_text
    assert "not a token or secret" in help_text.lower()
    assert "only change" in help_text.lower()
    caption_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("caption")
    ]
    assert any(
        "not a token or secret" in message.lower()
        for message in caption_messages
    )


def test_settings_page_wires_client_id_autofill_callback(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    [client_id_call] = [
        call
        for call in streamlit_recorder.calls_named("text_input")
        if call.args == ("Client ID",)
    ]
    assert client_id_call.kwargs["key"] == "cfg_client_id"
    assert (
        client_id_call.kwargs["on_change"]
        is settings_module._autofill_token_scope_from_client_id
    )


def test_scope_from_client_id_appends_default_suffix(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    assert (
        settings_module._scope_from_client_id("  client-abc  ")
        == "client-abc/.default"
    )
    assert settings_module._scope_from_client_id("   ") == ""


def test_resolve_token_scope_rules(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    # Default scope + new client ID -> derived.
    assert (
        settings_module._resolve_token_scope(
            "client-abc", ADME_RESOURCE_SCOPE, ""
        )
        == "client-abc/.default"
    )
    # Default scope but unchanged saved client ID -> preserved.
    assert (
        settings_module._resolve_token_scope(
            "client-abc", ADME_RESOURCE_SCOPE, "client-abc"
        )
        == ADME_RESOURCE_SCOPE
    )
    # Blank scope is preserved for backend fallback.
    assert (
        settings_module._resolve_token_scope("client-abc", "   ", "") == "   "
    )
    # Custom scope is preserved.
    assert (
        settings_module._resolve_token_scope(
            "client-abc", "https://x/.default", ""
        )
        == "https://x/.default"
    )
    # Default scope with no client ID stays at the default.
    assert (
        settings_module._resolve_token_scope("", ADME_RESOURCE_SCOPE, "")
        == ADME_RESOURCE_SCOPE
    )


def test_autofill_token_scope_fills_default_scope(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    streamlit_recorder.session_state["cfg_client_id"] = (
        "22222222-2222-2222-2222-222222222222"
    )
    streamlit_recorder.session_state["cfg_token_scope"] = ADME_RESOURCE_SCOPE

    settings_module._autofill_token_scope_from_client_id()

    assert (
        streamlit_recorder.session_state["cfg_token_scope"]
        == "22222222-2222-2222-2222-222222222222/.default"
    )


def test_autofill_token_scope_preserves_custom_scope(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    streamlit_recorder.session_state["cfg_client_id"] = (
        "22222222-2222-2222-2222-222222222222"
    )
    streamlit_recorder.session_state["cfg_token_scope"] = (
        "https://custom.energy.azure.com/.default"
    )

    settings_module._autofill_token_scope_from_client_id()

    assert (
        streamlit_recorder.session_state["cfg_token_scope"]
        == "https://custom.energy.azure.com/.default"
    )


def test_autofill_token_scope_overwrites_previous_autofill(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    streamlit_recorder.session_state["cfg_client_id"] = "aaaa/.bad"
    streamlit_recorder.session_state["cfg_client_id"] = "client-one"
    streamlit_recorder.session_state["cfg_token_scope"] = "client-zero/.default"
    streamlit_recorder.session_state["cfg_token_scope_autofilled"] = (
        "client-zero/.default"
    )

    settings_module._autofill_token_scope_from_client_id()

    assert (
        streamlit_recorder.session_state["cfg_token_scope"]
        == "client-one/.default"
    )


def test_autofill_token_scope_ignores_blank_client_id(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    streamlit_recorder.session_state["cfg_client_id"] = "   "
    streamlit_recorder.session_state["cfg_token_scope"] = ADME_RESOURCE_SCOPE

    settings_module._autofill_token_scope_from_client_id()

    assert (
        streamlit_recorder.session_state["cfg_token_scope"]
        == ADME_RESOURCE_SCOPE
    )


def test_settings_page_save_derives_token_scope_from_client_id(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.widget_values.update(
        {
            "ADME endpoint": "https://example.energy.azure.com",
            "Tenant ID": "11111111-1111-1111-1111-111111111111",
            "Client ID": "22222222-2222-2222-2222-222222222222",
            "Data partition ID": "example-opendes",
            "Authentication method": AuthMethod.SERVICE_PRINCIPAL,
            "Client secret": "super-secret",
        }
    )
    streamlit_recorder.submit_responses["Save Settings"] = True
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    saved_connection = streamlit_recorder.session_state[CONNECTION_KEY]
    assert isinstance(saved_connection, ADMEConnection)
    assert (
        saved_connection.token_scope
        == "22222222-2222-2222-2222-222222222222/.default"
    )


def test_settings_page_loads_persisted_profile_before_rendering_form(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    healthy_service_results: list[ServiceHealthResult],
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    def fake_load_persisted_connection_state(
        session_state: dict[str, object],
    ) -> StorageSyncStatus:
        session_state[CONNECTION_KEY] = _user_connection()
        session_state[HEALTH_RESULTS_KEY] = healthy_service_results
        return StorageSyncStatus(
            available=True,
            message="Loaded saved connection settings and latest validation.",
            severity="info",
            profile_loaded=True,
            health_loaded=True,
        )

    monkeypatch.setattr(
        settings_module,
        "load_persisted_connection_state",
        fake_load_persisted_connection_state,
    )
    monkeypatch.setattr(
        settings_module,
        "start_user_auth_flow",
        lambda connection: _flow_start(),
    )

    settings_module.main()

    assert streamlit_recorder.session_state[CONNECTION_KEY] == _user_connection()
    assert streamlit_recorder.session_state[HEALTH_RESULTS_KEY] == (
        healthy_service_results
    )
    [endpoint_call] = [
        call
        for call in streamlit_recorder.calls_named("text_input")
        if call.args == ("ADME endpoint",)
    ]
    assert endpoint_call.kwargs["value"] == _user_connection().endpoint
    assert any(
        call.args == ("Loaded saved connection settings and latest validation.",)
        for call in streamlit_recorder.calls_named("info")
    )


def test_settings_page_tests_connection_and_stores_results(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    healthy_service_results: list[ServiceHealthResult],
) -> None:
    streamlit_recorder.widget_values.update(
        {
            "ADME endpoint": "https://example.energy.azure.com",
            "Tenant ID": "11111111-1111-1111-1111-111111111111",
            "Client ID": "22222222-2222-2222-2222-222222222222",
            "Token scope": " https://custom.energy.azure.com/.default ",
            "Data partition ID": "example-opendes",
            "Authentication method": AuthMethod.SERVICE_PRINCIPAL,
            "Client secret": "super-secret",
        }
    )
    streamlit_recorder.submit_responses["Save Settings"] = True
    streamlit_recorder.submit_responses["Test Connection"] = True
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    captured: dict[str, object] = {}

    def fake_get_token(connection: ADMEConnection) -> str:
        captured["connection"] = connection
        return "test-token"

    def fake_check_all(
        connection: ADMEConnection, token: str
    ) -> list[ServiceHealthResult]:
        captured["token"] = token
        captured["health_connection"] = connection
        return healthy_service_results

    def fake_persist_connection_profile(
        connection: ADMEConnection,
    ) -> StorageSyncStatus:
        captured["persisted_profile"] = connection
        return StorageSyncStatus(available=True)

    def fake_persist_health_run(
        connection: ADMEConnection,
        results: list[ServiceHealthResult],
    ) -> StorageSyncStatus:
        captured["persisted_health_connection"] = connection
        captured["persisted_health_results"] = results
        return StorageSyncStatus(available=True)

    monkeypatch.setattr(settings_module, "get_token", fake_get_token)
    monkeypatch.setattr(settings_module, "check_all", fake_check_all)
    monkeypatch.setattr(
        settings_module,
        "persist_connection_profile",
        fake_persist_connection_profile,
    )
    monkeypatch.setattr(
        settings_module,
        "persist_health_run",
        fake_persist_health_run,
    )

    settings_module.main()

    saved_connection = streamlit_recorder.session_state[CONNECTION_KEY]
    assert isinstance(saved_connection, ADMEConnection)
    assert saved_connection.auth_method == AuthMethod.SERVICE_PRINCIPAL
    assert saved_connection.client_secret == "super-secret"
    assert saved_connection.token_scope == "https://custom.energy.azure.com/.default"
    assert (
        streamlit_recorder.session_state[HEALTH_RESULTS_KEY]
        == healthy_service_results
    )
    assert captured["token"] == "test-token"
    assert captured["connection"] == saved_connection
    assert captured["health_connection"] == saved_connection
    persisted_profile = captured["persisted_profile"]
    assert isinstance(persisted_profile, ADMEConnection)
    assert persisted_profile.client_secret == ""
    assert persisted_profile.auth_method == AuthMethod.SERVICE_PRINCIPAL
    persisted_health_connection = captured["persisted_health_connection"]
    assert isinstance(persisted_health_connection, ADMEConnection)
    assert persisted_health_connection.client_secret == ""
    assert captured["persisted_health_results"] == healthy_service_results


def test_settings_page_save_persists_non_secret_profile(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.widget_values.update(
        {
            "ADME endpoint": "https://example.energy.azure.com",
            "Tenant ID": "11111111-1111-1111-1111-111111111111",
            "Client ID": "22222222-2222-2222-2222-222222222222",
            "Data partition ID": "example-opendes",
            "Authentication method": AuthMethod.SERVICE_PRINCIPAL,
            "Client secret": "super-secret",
        }
    )
    streamlit_recorder.submit_responses["Save Settings"] = True
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    captured: dict[str, object] = {}

    def fake_persist_connection_profile(
        connection: ADMEConnection,
    ) -> StorageSyncStatus:
        captured["persisted_profile"] = connection
        return StorageSyncStatus(available=True)

    monkeypatch.setattr(
        settings_module,
        "persist_connection_profile",
        fake_persist_connection_profile,
    )

    settings_module.main()

    session_connection = streamlit_recorder.session_state[CONNECTION_KEY]
    assert isinstance(session_connection, ADMEConnection)
    assert session_connection.client_secret == "super-secret"
    persisted_profile = captured["persisted_profile"]
    assert isinstance(persisted_profile, ADMEConnection)
    assert persisted_profile.client_secret == ""
    assert persisted_profile.endpoint == session_connection.endpoint
    assert any(
        call.args
        == (
            "Connection settings saved persistently. Service-principal client "
            "secret is saved in your OS credential store.",
        )
        for call in streamlit_recorder.calls_named("success")
    )


def test_settings_page_surfaces_persistence_error_and_skips_validation(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.widget_values.update(
        {
            "ADME endpoint": "https://example.energy.azure.com",
            "Tenant ID": "11111111-1111-1111-1111-111111111111",
            "Client ID": "22222222-2222-2222-2222-222222222222",
            "Data partition ID": "example-opendes",
            "Authentication method": AuthMethod.SERVICE_PRINCIPAL,
            "Client secret": "super-secret",
        }
    )
    streamlit_recorder.submit_responses["Save Settings"] = True
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise settings_store.SettingsStoreError("keyring locked")

    def fail_get_token(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("validation should not run after save failure")

    monkeypatch.setattr(settings_module, "save_connection", fail_save)
    monkeypatch.setattr(settings_module, "get_token", fail_get_token)

    settings_module.main()

    error_messages = [call.args[0] for call in streamlit_recorder.calls_named("error")]
    assert "Connection settings could not be saved: keyring locked" in error_messages
    assert streamlit_recorder.session_state[CONNECTION_KEY] is None
    assert streamlit_recorder.session_state[HEALTH_RESULTS_KEY] == []


def test_settings_page_allows_blank_token_scope_for_backend_fallback(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.widget_values.update(
        {
            "ADME endpoint": "https://example.energy.azure.com",
            "Tenant ID": "11111111-1111-1111-1111-111111111111",
            "Client ID": "22222222-2222-2222-2222-222222222222",
            "Token scope": "   ",
            "Data partition ID": "example-opendes",
            "Authentication method": AuthMethod.SERVICE_PRINCIPAL,
            "Client secret": "super-secret",
        }
    )
    streamlit_recorder.submit_responses["Save Settings"] = True
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    saved_connection = streamlit_recorder.session_state[CONNECTION_KEY]
    assert isinstance(saved_connection, ADMEConnection)
    assert saved_connection.token_scope == ""
    assert saved_connection.scope == ADME_RESOURCE_SCOPE


def test_settings_page_clears_stale_health_results_on_save(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    healthy_service_results: list[ServiceHealthResult],
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = ADMEConnection(
        endpoint="https://old.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="old-opendes",
    )
    streamlit_recorder.session_state[HEALTH_RESULTS_KEY] = healthy_service_results
    streamlit_recorder.widget_values.update(
        {
            "ADME endpoint": "https://new.energy.azure.com",
            "Tenant ID": "11111111-1111-1111-1111-111111111111",
            "Client ID": "22222222-2222-2222-2222-222222222222",
            "Data partition ID": "new-opendes",
            "Authentication method": AuthMethod.USER_IMPERSONATION,
        }
    )
    streamlit_recorder.submit_responses["Save Settings"] = True
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    monkeypatch.setattr(
        settings_module,
        "start_user_auth_flow",
        lambda connection: _flow_start(),
    )

    settings_module.main()

    assert streamlit_recorder.session_state[HEALTH_RESULTS_KEY] == []
    info_messages = streamlit_recorder.calls_named("info")
    assert any(
        call.args
        == (
            "Sign in with Microsoft to enable Test Connection for this "
            "user-impersonation connection.",
        )
        for call in info_messages
    )


def test_settings_page_clears_auth_and_health_when_token_scope_changes(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    healthy_service_results: list[ServiceHealthResult],
) -> None:
    old_flow = _named_flow_start("old")
    new_flow = _named_flow_start("new")
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection(
        "https://old.energy.azure.com/.default",
    )
    streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] = old_flow
    streamlit_recorder.session_state[USER_AUTH_STATE_KEY] = _auth_state()
    streamlit_recorder.session_state[HEALTH_RESULTS_KEY] = healthy_service_results
    streamlit_recorder.session_state[HEALTH_ERROR_KEY] = "Old validation error."
    streamlit_recorder.widget_values.update(
        {
            "ADME endpoint": "https://example.energy.azure.com",
            "Tenant ID": "11111111-1111-1111-1111-111111111111",
            "Client ID": "22222222-2222-2222-2222-222222222222",
            "Token scope": "https://new.energy.azure.com/.default",
            "Data partition ID": "example-opendes",
            "Authentication method": AuthMethod.USER_IMPERSONATION,
        }
    )
    streamlit_recorder.submit_responses["Save Settings"] = True
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    captured: dict[str, object] = {}

    def fake_start_user_auth_flow(connection: ADMEConnection) -> UserAuthFlowStart:
        captured["connection"] = connection
        return new_flow

    monkeypatch.setattr(
        settings_module,
        "start_user_auth_flow",
        fake_start_user_auth_flow,
    )

    settings_module.main()

    expected_connection = _user_connection("https://new.energy.azure.com/.default")
    assert streamlit_recorder.session_state[CONNECTION_KEY] == expected_connection
    assert streamlit_recorder.session_state[USER_AUTH_STATE_KEY] is None
    assert streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] == new_flow
    assert streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] != old_flow
    assert streamlit_recorder.session_state[HEALTH_RESULTS_KEY] == []
    assert streamlit_recorder.session_state[HEALTH_ERROR_KEY] == ""
    assert captured["connection"] == expected_connection


def test_settings_page_keeps_test_enabled_with_unsaved_changes_when_signed_in(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_STATE_KEY] = _auth_state()
    streamlit_recorder.widget_values.update(
        {
            "ADME endpoint": "https://example.energy.azure.com",
            "Tenant ID": "11111111-1111-1111-1111-111111111111",
            "Client ID": "22222222-2222-2222-2222-222222222222",
            "Token scope": "https://new.energy.azure.com/.default",
            "Data partition ID": "example-opendes",
            "Authentication method": AuthMethod.USER_IMPERSONATION,
        }
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    [test_button_call] = [
        call
        for call in streamlit_recorder.calls_named("form_submit_button")
        if call.args == ("Test Connection",)
    ]
    assert test_button_call.kwargs["disabled"] is False
    caption_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("caption")
    ]
    assert any(
        "unsaved changes" in message.lower() for message in caption_messages
    )


def test_settings_page_auth_method_radio_renders_in_identity_section(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    radio_calls = streamlit_recorder.calls_named("radio")
    subheader_calls = [
        call.args[0] for call in streamlit_recorder.calls_named("subheader")
    ]
    assert any(call.args[0] == "Authentication method" for call in radio_calls)
    assert "Identity & authentication" in subheader_calls


def test_settings_page_paste_command_has_no_resource_flag(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_STATE_KEY] = _auth_state()
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    code_blocks = [
        call.args[0] for call in streamlit_recorder.calls_named("code")
    ]
    assert any(
        "az account get-access-token --query accessToken -o tsv" in block
        for block in code_blocks
    )
    assert all("--resource" not in block for block in code_blocks)


def test_settings_page_warns_when_session_token_expired(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_STATE_KEY] = UserAuthState(
        access_token="placeholder-user-token",
        expires_at=1,
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    info_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("info")
    ]
    assert any("expired" in message.lower() for message in info_messages)


def test_settings_page_shows_token_validity_for_fresh_token(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as _time

    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_STATE_KEY] = UserAuthState(
        access_token="placeholder-user-token",
        expires_at=int(_time.time()) + 3600,
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    caption_messages = [
        call.args[0] for call in streamlit_recorder.calls_named("caption")
    ]
    assert any("Token valid until" in message for message in caption_messages)


def test_settings_page_shows_sign_in_for_saved_user_connection(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    captured: dict[str, object] = {}

    def fake_start_user_auth_flow(connection: ADMEConnection) -> UserAuthFlowStart:
        captured["connection"] = connection
        return _flow_start()

    monkeypatch.setattr(
        settings_module,
        "start_user_auth_flow",
        fake_start_user_auth_flow,
    )

    settings_module.main()

    [link_button_call] = streamlit_recorder.calls_named("link_button")
    assert link_button_call.args == ("Sign In", _flow_start().authorization_url)
    assert link_button_call.kwargs["type"] == "primary"
    [test_button_call] = [
        call
        for call in streamlit_recorder.calls_named("form_submit_button")
        if call.args == ("Test Connection",)
    ]
    assert test_button_call.kwargs["disabled"] is True
    assert streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] == _flow_start()
    assert captured["connection"] == _user_connection()


def test_settings_page_uses_session_auth_state_for_user_connection_test(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    healthy_service_results: list[ServiceHealthResult],
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_STATE_KEY] = _auth_state()
    streamlit_recorder.submit_responses["Test Connection"] = True
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    captured: dict[str, object] = {}

    def fake_get_token(
        connection: ADMEConnection,
        user_auth_state: UserAuthState | None = None,
    ) -> str:
        captured["connection"] = connection
        captured["user_auth_state"] = user_auth_state
        return "placeholder-user-token"

    def fake_check_all(
        connection: ADMEConnection,
        token: str,
    ) -> list[ServiceHealthResult]:
        captured["health_connection"] = connection
        captured["token"] = token
        return healthy_service_results

    monkeypatch.setattr(settings_module, "get_token", fake_get_token)
    monkeypatch.setattr(settings_module, "check_all", fake_check_all)

    settings_module.main()

    assert captured["connection"] == _user_connection()
    assert captured["user_auth_state"] == _auth_state()
    assert captured["token"] == "placeholder-user-token"
    assert streamlit_recorder.session_state[HEALTH_RESULTS_KEY] == (
        healthy_service_results
    )
    [test_button_call] = [
        call
        for call in streamlit_recorder.calls_named("form_submit_button")
        if call.args == ("Test Connection",)
    ]
    assert test_button_call.kwargs["disabled"] is False


def test_settings_page_sign_out_clears_user_auth_and_health(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    healthy_service_results: list[ServiceHealthResult],
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_STATE_KEY] = _auth_state()
    streamlit_recorder.session_state[HEALTH_RESULTS_KEY] = healthy_service_results
    streamlit_recorder.button_responses["Sign Out"] = True
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    assert streamlit_recorder.session_state[USER_AUTH_STATE_KEY] is None
    assert streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] is None
    assert streamlit_recorder.session_state[HEALTH_RESULTS_KEY] == []


def test_settings_page_consumes_oauth_callback_once(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    healthy_service_results: list[ServiceHealthResult],
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] = _flow_start()
    streamlit_recorder.session_state[HEALTH_RESULTS_KEY] = healthy_service_results
    streamlit_recorder.query_params.update(
        {
            "code": "placeholder-auth-code",
            "state": "placeholder-state",
        }
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    captured: dict[str, object] = {}
    complete_call_count = 0

    def fake_complete_user_auth_flow(
        connection: ADMEConnection,
        flow: object,
        callback_params: dict[str, object],
    ) -> UserAuthState:
        nonlocal complete_call_count
        complete_call_count += 1
        captured["connection"] = connection
        captured["flow"] = flow
        captured["callback_params"] = callback_params
        return _auth_state()

    monkeypatch.setattr(
        settings_module,
        "complete_user_auth_flow",
        fake_complete_user_auth_flow,
    )

    settings_module.main()
    settings_module.main()

    assert complete_call_count == 1
    assert captured["connection"] == _user_connection()
    assert captured["flow"] == _flow_start()
    assert captured["callback_params"] == {
        "code": "placeholder-auth-code",
        "state": "placeholder-state",
    }
    assert streamlit_recorder.session_state[USER_AUTH_STATE_KEY] == _auth_state()
    assert streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] is None
    assert streamlit_recorder.session_state[HEALTH_RESULTS_KEY] == []
    assert streamlit_recorder.query_params == {}
    assert streamlit_recorder.query_params.clear_count == 1


def test_settings_page_rejects_callback_without_pending_flow(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.query_params.update(
        {
            "code": "placeholder-auth-code",
            "state": "placeholder-state",
        }
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    def fail_if_called(*args: object, **kwargs: object) -> UserAuthState:
        raise AssertionError("complete_user_auth_flow should not be called")

    start_call_count = 0

    def fake_start_user_auth_flow(connection: ADMEConnection) -> UserAuthFlowStart:
        nonlocal start_call_count
        assert connection == _user_connection()
        start_call_count += 1
        return _flow_start()

    monkeypatch.setattr(
        settings_module,
        "complete_user_auth_flow",
        fail_if_called,
    )
    monkeypatch.setattr(
        settings_module,
        "start_user_auth_flow",
        fake_start_user_auth_flow,
    )

    settings_module.main()

    error_messages = [call.args[0] for call in streamlit_recorder.calls_named("error")]
    assert (
        "Sign-in callback expired or was already used. Start Sign In again."
    ) in error_messages
    assert streamlit_recorder.query_params == {}
    assert streamlit_recorder.query_params.clear_count == 1
    assert start_call_count == 1
    assert streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] == _flow_start()


def test_settings_page_auth_denial_clears_stale_pending_flow_and_query(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_flow = _named_flow_start("old")
    new_flow = _named_flow_start("new")
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] = old_flow
    streamlit_recorder.query_params.update(
        {
            "error": "access_denied",
            "error_description": "do not show this raw callback detail",
            "state": "old-state",
        }
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    start_call_count = 0

    def fake_start_user_auth_flow(connection: ADMEConnection) -> UserAuthFlowStart:
        nonlocal start_call_count
        assert connection == _user_connection()
        start_call_count += 1
        return new_flow

    monkeypatch.setattr(
        settings_module,
        "start_user_auth_flow",
        fake_start_user_auth_flow,
    )

    settings_module.main()

    error_messages = [call.args[0] for call in streamlit_recorder.calls_named("error")]
    assert (
        "User sign-in failed (access_denied). Start sign-in again."
    ) in error_messages
    assert all("raw callback detail" not in message for message in error_messages)
    assert streamlit_recorder.query_params == {}
    assert streamlit_recorder.query_params.clear_count == 1
    assert start_call_count == 1
    assert streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] == new_flow
    [link_button_call] = streamlit_recorder.calls_named("link_button")
    assert link_button_call.args == ("Sign In", new_flow.authorization_url)


def test_settings_page_state_mismatch_clears_stale_pending_flow(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_flow = _named_flow_start("old")
    new_flow = _named_flow_start("new")
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] = old_flow
    streamlit_recorder.query_params.update(
        {
            "code": "placeholder-auth-code",
            "state": "wrong-state",
        }
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    captured: dict[str, object] = {}
    start_call_count = 0

    def fake_complete_user_auth_flow(
        connection: ADMEConnection,
        flow: object,
        callback_params: dict[str, object],
    ) -> UserAuthState:
        captured["connection"] = connection
        captured["flow"] = flow
        captured["callback_params"] = callback_params
        raise AuthenticationError(
            "User sign-in callback did not match the pending authentication "
            "flow. Start sign-in again."
        )

    def fake_start_user_auth_flow(connection: ADMEConnection) -> UserAuthFlowStart:
        nonlocal start_call_count
        assert connection == _user_connection()
        start_call_count += 1
        return new_flow

    monkeypatch.setattr(
        settings_module,
        "complete_user_auth_flow",
        fake_complete_user_auth_flow,
    )
    monkeypatch.setattr(
        settings_module,
        "start_user_auth_flow",
        fake_start_user_auth_flow,
    )

    settings_module.main()

    error_messages = [call.args[0] for call in streamlit_recorder.calls_named("error")]
    assert (
        "User sign-in callback did not match the pending authentication flow. "
        "Start sign-in again."
    ) in error_messages
    assert captured == {
        "connection": _user_connection(),
        "flow": old_flow,
        "callback_params": {
            "code": "placeholder-auth-code",
            "state": "wrong-state",
        },
    }
    assert streamlit_recorder.query_params == {}
    assert streamlit_recorder.query_params.clear_count == 1
    assert start_call_count == 1
    assert streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] == new_flow


def test_settings_page_token_exchange_failure_clears_stale_pending_flow(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_flow = _named_flow_start("old")
    new_flow = _named_flow_start("new")
    streamlit_recorder.session_state[CONNECTION_KEY] = _user_connection()
    streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] = old_flow
    streamlit_recorder.query_params.update(
        {
            "code": "placeholder-auth-code",
            "state": "old-state",
            "error_description": "do not show this raw callback detail",
        }
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    captured: dict[str, object] = {}
    start_call_count = 0

    def fake_complete_user_auth_flow(
        connection: ADMEConnection,
        flow: object,
        callback_params: dict[str, object],
    ) -> UserAuthState:
        captured["connection"] = connection
        captured["flow"] = flow
        captured["callback_params"] = callback_params
        raise AuthenticationError(
            "User sign-in failed (access_denied). Start sign-in again."
        )

    def fake_start_user_auth_flow(connection: ADMEConnection) -> UserAuthFlowStart:
        nonlocal start_call_count
        assert connection == _user_connection()
        start_call_count += 1
        return new_flow

    monkeypatch.setattr(
        settings_module,
        "complete_user_auth_flow",
        fake_complete_user_auth_flow,
    )
    monkeypatch.setattr(
        settings_module,
        "start_user_auth_flow",
        fake_start_user_auth_flow,
    )

    settings_module.main()

    error_messages = [call.args[0] for call in streamlit_recorder.calls_named("error")]
    assert (
        "User sign-in failed (access_denied). Start sign-in again."
    ) in error_messages
    assert all("raw callback detail" not in message for message in error_messages)
    assert captured == {
        "connection": _user_connection(),
        "flow": old_flow,
        "callback_params": {
            "code": "placeholder-auth-code",
            "state": "old-state",
            "error_description": "do not show this raw callback detail",
        },
    }
    assert streamlit_recorder.query_params == {}
    assert streamlit_recorder.query_params.clear_count == 1
    assert start_call_count == 1
    assert streamlit_recorder.session_state[USER_AUTH_FLOW_KEY] == new_flow


def test_settings_page_keeps_generic_retry_guidance_for_service_principal_errors(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamlit_recorder.widget_values.update(
        {
            "ADME endpoint": "https://example.energy.azure.com",
            "Tenant ID": "11111111-1111-1111-1111-111111111111",
            "Client ID": "22222222-2222-2222-2222-222222222222",
            "Data partition ID": "example-opendes",
            "Authentication method": AuthMethod.SERVICE_PRINCIPAL,
            "Client secret": "super-secret",
        }
    )
    streamlit_recorder.submit_responses["Save Settings"] = True
    streamlit_recorder.submit_responses["Test Connection"] = True
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    def fake_get_token(connection: ADMEConnection) -> str:
        del connection
        raise RuntimeError("Client secret was rejected.")

    monkeypatch.setattr(settings_module, "get_token", fake_get_token)

    settings_module.main()

    error_messages = [call.args[0] for call in streamlit_recorder.calls_named("error")]
    assert (
        "Connection test failed: Client secret was rejected. Run Test "
        "Connection again to retry."
    ) in error_messages
    assert (
        sum(
            "Client secret was rejected" in message
            for message in error_messages
        )
        == 1
    )
    assert all("separate sign-in tab" not in message for message in error_messages)


def test_settings_page_renders_status_matrix_for_latest_results(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
    degraded_service_results: list[ServiceHealthResult],
) -> None:
    streamlit_recorder.session_state[CONNECTION_KEY] = ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
    )
    streamlit_recorder.session_state[USER_AUTH_STATE_KEY] = _auth_state()
    streamlit_recorder.session_state[HEALTH_RESULTS_KEY] = degraded_service_results
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)

    settings_module.main()

    [dataframe_call] = streamlit_recorder.calls_named("dataframe")
    rows = dataframe_call.args[0]
    assert len(rows) == len(degraded_service_results)
    assert any(row["State"] == "⚠️ Unhealthy" for row in rows)
    assert any(row["State"] == "❌ Error" for row in rows)
