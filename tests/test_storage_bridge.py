"""Tests for the UI bridge around optional persistent storage."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.connection_state import CONNECTION_KEY, HEALTH_RESULTS_KEY
from app.models.connection import (
    ADME_RESOURCE_SCOPE,
    ADMEConnection,
    AuthMethod,
    ServiceHealthResult,
)
from app.services.auth import UserAuthFlowStart
from app.storage_bridge import StorageSyncStatus
from tests.support.streamlit_recorder import StreamlitRecorder

SETTINGS_PAGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "pages"
    / "1_⚙️_Instance_Configuration.py"
)


def _stored_connection_payload() -> dict[str, object]:
    return {
        "endpoint": "https://stored.energy.azure.com",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "22222222-2222-2222-2222-222222222222",
        "data_partition_id": "stored-opendes",
        "token_scope": "https://stored.energy.azure.com/.default",
        "auth_method": AuthMethod.USER_IMPERSONATION.value,
        "client_secret": "must-not-load",
    }


def _stored_health_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "service_name": "Storage",
                "path": "/api/storage/v2/query/kinds?limit=1",
                "status": "healthy",
                "status_code": 200,
                "response_time_ms": 12.5,
                "error_message": "",
            }
        ],
    }


def _service_principal_connection() -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        token_scope=ADME_RESOURCE_SCOPE,
        auth_method=AuthMethod.SERVICE_PRINCIPAL,
        client_secret="placeholder-client-secret",
    )


def _install_fake_storage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: object | None = None,
    health_run: object | None = None,
) -> dict[str, object]:
    calls: dict[str, object] = {"initialize_count": 0}
    storage_root = ModuleType("app.storage")
    repositories_root = ModuleType("app.storage.repositories")
    profiles = ModuleType("app.storage.repositories.connection_profiles")
    health_runs = ModuleType("app.storage.repositories.health_runs")
    setattr(storage_root, "__path__", [])
    setattr(repositories_root, "__path__", [])

    def initialize_storage() -> None:
        initialize_count = calls["initialize_count"]
        assert isinstance(initialize_count, int)
        calls["initialize_count"] = initialize_count + 1

    def load_active_connection_profile() -> object | None:
        return profile

    def save_active_connection_profile(
        connection: ADMEConnection,
        set_active: bool = False,
    ) -> None:
        calls["saved_profile"] = connection
        calls["saved_profile_set_active"] = set_active

    def load_latest_health_run(connection: ADMEConnection) -> object | None:
        calls["latest_health_connection"] = connection
        return health_run

    def record_health_run(
        connection: ADMEConnection,
        results: list[ServiceHealthResult],
    ) -> None:
        calls["recorded_health_connection"] = connection
        calls["recorded_health_results"] = results

    def delete_profile(profile_id: str) -> bool:
        calls["deleted_profile_id"] = profile_id
        return True

    def clear_active_profile() -> None:
        calls["active_profile_cleared"] = True

    setattr(storage_root, "initialize_storage", initialize_storage)
    setattr(profiles, "load_active_connection_profile", load_active_connection_profile)
    setattr(profiles, "save_active_connection_profile", save_active_connection_profile)
    setattr(profiles, "delete_profile", delete_profile)
    setattr(profiles, "clear_active_profile", clear_active_profile)
    setattr(health_runs, "load_latest_health_run", load_latest_health_run)
    setattr(health_runs, "record_health_run", record_health_run)

    for module in (storage_root, repositories_root, profiles, health_runs):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return calls


def _load_settings_module(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    monkeypatch.setitem(sys.modules, "streamlit", streamlit_recorder)
    module_name = "tests.generated_storage_bridge_settings_page"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, SETTINGS_PAGE_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_main_module(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    monkeypatch.setitem(sys.modules, "streamlit", streamlit_recorder)
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def test_load_persisted_connection_state_loads_profile_and_health_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_storage(
        monkeypatch,
        profile=_stored_connection_payload(),
        health_run=_stored_health_payload(),
    )
    from app.storage_bridge import load_persisted_connection_state

    session_state: dict[str, object] = {}
    status = load_persisted_connection_state(session_state)

    assert status == StorageSyncStatus(
        available=True,
        message=(
            "Loaded saved connection settings and latest validation from "
            "persistent storage. Service-principal secrets load from the OS "
            "credential store; user sign-in still belongs to this Streamlit "
            "session."
        ),
        severity="info",
        profile_loaded=True,
        health_loaded=True,
    )
    loaded_connection = session_state[CONNECTION_KEY]
    assert isinstance(loaded_connection, ADMEConnection)
    assert loaded_connection.endpoint == "https://stored.energy.azure.com"
    assert loaded_connection.data_partition_id == "stored-opendes"
    assert loaded_connection.client_secret == ""
    assert session_state[HEALTH_RESULTS_KEY] == [
        ServiceHealthResult(
            service_name="Storage",
            path="/api/storage/v2/query/kinds?limit=1",
            status="healthy",
            status_code=200,
            response_time_ms=12.5,
            error_message="",
        )
    ]


def test_persist_connection_profile_strips_client_secret_before_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_storage(monkeypatch)
    from app.storage_bridge import persist_connection_profile

    status = persist_connection_profile(_service_principal_connection())

    assert status == StorageSyncStatus(available=True)
    saved_profile = calls["saved_profile"]
    assert isinstance(saved_profile, ADMEConnection)
    assert saved_profile.client_secret == ""
    assert saved_profile.auth_method == AuthMethod.SERVICE_PRINCIPAL
    assert saved_profile.endpoint == "https://example.energy.azure.com"
    assert calls["saved_profile_set_active"] is True


def test_persist_health_run_strips_client_secret_before_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_storage(monkeypatch)
    from app.storage_bridge import persist_health_run

    results = [
        ServiceHealthResult(
            service_name="Search",
            path="/api/search/v2/query",
            status="healthy",
            status_code=200,
            response_time_ms=21.0,
        )
    ]
    status = persist_health_run(_service_principal_connection(), results)

    assert status == StorageSyncStatus(available=True)
    saved_connection = calls["recorded_health_connection"]
    assert isinstance(saved_connection, ADMEConnection)
    assert saved_connection.client_secret == ""
    assert calls["recorded_health_results"] == results


def test_forget_persisted_connection_profile_deletes_active_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_profile = SimpleNamespace(
        id="profile-1",
        connection=_stored_connection_payload(),
    )
    calls = _install_fake_storage(monkeypatch, profile=active_profile)
    from app.storage_bridge import forget_persisted_connection_profile

    status = forget_persisted_connection_profile()

    assert status == StorageSyncStatus(available=True)
    assert calls["deleted_profile_id"] == "profile-1"
    assert calls["active_profile_cleared"] is True


def test_settings_and_welcome_load_fake_storage_without_reentering_fields(
    streamlit_recorder: StreamlitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_storage(
        monkeypatch,
        profile=_stored_connection_payload(),
        health_run=_stored_health_payload(),
    )
    settings_module = _load_settings_module(streamlit_recorder, monkeypatch)
    monkeypatch.setattr(
        settings_module,
        "start_user_auth_flow",
        lambda connection: UserAuthFlowStart(
            authorization_url="https://login.example.test/authorize",
            flow={"state": "stored-state"},
        ),
    )

    settings_module.main()

    text_values = {
        call.args[0]: call.kwargs["value"]
        for call in streamlit_recorder.calls_named("text_input")
    }
    assert text_values["ADME endpoint"] == "https://stored.energy.azure.com"
    assert text_values["Tenant ID"] == "11111111-1111-1111-1111-111111111111"
    assert (
        streamlit_recorder.session_state["cfg_client_id"]
        == "22222222-2222-2222-2222-222222222222"
    )
    assert (
        streamlit_recorder.session_state["cfg_token_scope"]
        == "https://stored.energy.azure.com/.default"
    )
    assert text_values["Data partition ID"] == "stored-opendes"
    assert "Client secret" not in text_values

    welcome_recorder = StreamlitRecorder()
    main_module = _load_main_module(welcome_recorder, monkeypatch)
    main_module.main()

    warning_messages = [
        call.args[0] for call in welcome_recorder.calls_named("warning")
    ]
    assert "No ADME connection is configured for this session." not in warning_messages
    assert any(
        "https://stored.energy.azure.com" in call.args[0]
        for call in welcome_recorder.calls_named("markdown")
    )


def test_storage_bridge_reports_warning_when_storage_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if importlib.util.find_spec("app.storage") is not None:
        pytest.skip("Persistent storage package is available in this branch.")
    for module_name in [
        "app.storage",
        "app.storage.repositories",
        "app.storage.repositories.connection_profiles",
        "app.storage.repositories.health_runs",
    ]:
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    from app.storage_bridge import load_persisted_connection_state

    status = load_persisted_connection_state({})

    assert status.available is False
    assert status.severity == "warning"
    assert "Persistent storage is not available" in status.message
