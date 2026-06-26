"""Instance configuration page for ADME connection setup and validation."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if PROJECT_ROOT not in {Path(path or ".").resolve() for path in sys.path}:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # type: ignore[import-not-found]  # noqa: E402

from app.connection_state import (  # noqa: E402
    clear_health_state,
    clear_pending_user_auth_flow,
    clear_user_auth_state,
    ensure_session_defaults,
    format_auth_method,
    format_overall_state,
    get_connection,
    get_health_error,
    get_health_results,
    get_overall_state,
    get_pending_user_auth_flow,
    get_user_auth_state,
    results_to_table_rows,
    save_connection,
    store_health_error,
    store_health_results,
    store_pending_user_auth_flow,
    store_user_auth_state,
    summarize_health,
)
from app.models.connection import (  # noqa: E402
    ADME_RESOURCE_SCOPE,
    ADMEConnection,
    AuthMethod,
    ServiceHealthResult,
)
from app.services import (  # noqa: E402
    settings_store,
    token_utils,
)
from app.services.auth import (  # noqa: E402
    AuthenticationError,
    complete_user_auth_flow,
    get_token,
    start_user_auth_flow,
    user_auth_state_from_pasted_token,
)
from app.services.health import check_all  # noqa: E402
from app.storage_bridge import (  # noqa: E402
    StorageSyncStatus,
    connection_profile_without_secret,
    load_persisted_connection_state,
    persist_connection_profile,
    persist_health_run,
)

OAUTH_CALLBACK_PARAM_KEYS = {
    "client_info",
    "code",
    "error",
    "error_description",
    "error_subcode",
    "error_uri",
    "session_state",
    "state",
}
USER_IMPERSONATION_GUIDANCE = (
    "Sign in with Microsoft for this user-impersonation connection. "
    "After sign-in completes, Test Connection is enabled for this session."
)
USER_IMPERSONATION_REFRESH_GUIDANCE = (
    "Sign in with Microsoft to enable Test Connection for this "
    "user-impersonation connection."
)
USER_IMPERSONATION_RETRY_GUIDANCE = (
    "Sign in with Microsoft, then run Test Connection again."
)
RETRY_CONNECTION_TEST_GUIDANCE = "Run Test Connection again to retry."
TOKEN_SCOPE_HELP = (
    "OAuth resource scope for ADME token acquisition (defaults to the ADME "
    "resource scope). "
    "This is configuration only—not a token or secret. "
    "Do not paste tokens, client secrets, or authorization codes here. "
    "Only change this if your Entra app registration or ADME deployment requires "
    "a custom OAuth scope."
)


def main() -> None:
    """Render the operator settings flow for ADME connectivity."""
    st.set_page_config(
        page_title="Instance Configuration · ADME Control Plane",
        page_icon="⚙️",
        layout="wide",
    )
    st.title("Instance Configuration")
    st.markdown(
        "Configure the ADME connection for this session and validate each "
        "OSDU service before starting an operator workflow."
    )
    st.caption(
        "Settings are saved persistently when storage is available. "
        "Service-principal client secrets are stored in your OS credential "
        "store; pending sign-in and access tokens stay in Streamlit session "
        "state. User impersonation requires sign-in for each Streamlit session."
    )

    ensure_session_defaults(st.session_state)
    _render_storage_status(load_persisted_connection_state(st.session_state))
    _consume_oauth_callback_once()
    st.markdown(
        f"**Current session state:** "
        f"{format_overall_state(get_overall_state(st.session_state))}"
    )
    existing_connection = get_connection(st.session_state)
    _render_connection_form(existing_connection)
    _render_latest_validation()


def _consume_oauth_callback_once() -> None:
    """Exchange OAuth callback params once, then clear them from the URL."""
    callback_params = _copy_oauth_callback_params()
    if not callback_params:
        return

    try:
        connection = get_connection(st.session_state)
        pending_flow = get_pending_user_auth_flow(st.session_state)
        if (
            connection is None
            or not connection.is_valid()
            or connection.auth_method != AuthMethod.USER_IMPERSONATION
        ):
            st.error(
                "Sign-in callback could not be matched to a valid "
                "user-impersonation connection. Save settings and sign in again."
            )
            return
        if pending_flow is None:
            st.error(
                "Sign-in callback expired or was already used. Start Sign In again."
            )
            return

        auth_state = complete_user_auth_flow(connection, pending_flow, callback_params)
        store_user_auth_state(st.session_state, auth_state)
        st.success("Sign-in complete. You can test the connection now.")
    except AuthenticationError as exc:
        st.error(str(exc))
    except Exception:  # noqa: BLE001 - never expose raw auth callback details
        st.error("User sign-in could not be completed. Start Sign In again.")
    finally:
        clear_pending_user_auth_flow(st.session_state)
        _clear_oauth_query_params()


def _copy_oauth_callback_params() -> dict[str, object]:
    """Copy OAuth callback params from Streamlit query state."""
    raw_params = _get_query_params()
    return {
        key: value
        for key, value in raw_params.items()
        if key in OAUTH_CALLBACK_PARAM_KEYS or key.startswith("error")
    }


def _get_query_params() -> dict[str, object]:
    """Return current query params from modern or legacy Streamlit APIs."""
    query_params = getattr(st, "query_params", None)
    if hasattr(query_params, "items"):
        query_params_with_items = cast(Any, query_params)
        return {str(key): value for key, value in query_params_with_items.items()}

    experimental_get_query_params = getattr(
        st,
        "experimental_get_query_params",
        None,
    )
    if callable(experimental_get_query_params):
        return {
            str(key): value
            for key, value in experimental_get_query_params().items()
        }
    return {}


def _clear_oauth_query_params() -> None:
    """Clear callback params so Streamlit reruns cannot replay an exchange."""
    query_params = getattr(st, "query_params", None)
    if hasattr(query_params, "clear"):
        cast(Any, query_params).clear()
        return

    experimental_set_query_params = getattr(
        st,
        "experimental_set_query_params",
        None,
    )
    if callable(experimental_set_query_params):
        experimental_set_query_params()


def _render_connection_form(existing_connection: ADMEConnection | None) -> None:
    """Render the configuration form and handle save or test actions."""
    auth_methods = list(AuthMethod)
    default_auth_method = (
        existing_connection.auth_method
        if existing_connection is not None
        else AuthMethod.USER_IMPERSONATION
    )

    with st.form("adme_connection_form"):
        endpoint = st.text_input(
            "ADME endpoint",
            value=existing_connection.endpoint if existing_connection else "",
            placeholder="https://contoso.energy.azure.com",
        )
        tenant_id = st.text_input(
            "Tenant ID",
            value=existing_connection.tenant_id if existing_connection else "",
            placeholder="11111111-1111-1111-1111-111111111111",
        )
        client_id = st.text_input(
            "Client ID",
            value=existing_connection.client_id if existing_connection else "",
            placeholder="22222222-2222-2222-2222-222222222222",
        )
        token_scope = st.text_input(
            "Token scope",
            value=(
                existing_connection.token_scope
                if existing_connection is not None
                else ADME_RESOURCE_SCOPE
            ),
            placeholder=ADME_RESOURCE_SCOPE,
            help=TOKEN_SCOPE_HELP,
        )
        st.caption(TOKEN_SCOPE_HELP)
        data_partition_id = st.text_input(
            "Data partition ID",
            value=(
                existing_connection.data_partition_id
                if existing_connection
                else ""
            ),
            placeholder="contoso-opendes",
        )
        auth_method = st.radio(
            "Authentication method",
            options=auth_methods,
            index=auth_methods.index(default_auth_method),
            format_func=format_auth_method,
        )

        client_secret = ""
        if auth_method == AuthMethod.SERVICE_PRINCIPAL:
            client_secret = st.text_input(
                "Client secret",
                value=(
                    existing_connection.client_secret
                    if existing_connection
                    and existing_connection.auth_method
                    == AuthMethod.SERVICE_PRINCIPAL
                    else ""
                ),
                type="password",
            )
            st.caption(
                "Client secret is masked and saved in your OS credential store "
                "(never in the settings database)."
            )
        else:
            st.info(USER_IMPERSONATION_GUIDANCE)

        draft_connection = ADMEConnection(
            endpoint=endpoint.strip(),
            tenant_id=tenant_id.strip(),
            client_id=client_id.strip(),
            data_partition_id=data_partition_id.strip(),
            token_scope=token_scope.strip(),
            auth_method=auth_method,
            client_secret=client_secret.strip(),
        )
        save_clicked = st.form_submit_button("Save Settings")
        test_clicked = st.form_submit_button(
            "Test Connection",
            type="primary",
            disabled=_test_connection_disabled(
                existing_connection,
                draft_connection,
            ),
        )

    if save_clicked or test_clicked:
        _handle_form_action(
            existing_connection=existing_connection,
            connection=draft_connection,
            test_clicked=test_clicked,
        )
    _render_user_auth_controls(get_connection(st.session_state))


def _handle_form_action(
    *,
    existing_connection: ADMEConnection | None,
    connection: ADMEConnection,
    test_clicked: bool,
) -> None:
    """Persist connection details and optionally run service validation."""
    if not connection.is_valid():
        st.error("Complete every required field before saving or testing.")
        return

    connection_changed = existing_connection != connection
    try:
        save_connection(st.session_state, connection)
    except settings_store.SettingsStoreError as exc:
        st.error(f"Connection settings could not be saved: {exc}")
        return
    profile_for_storage = connection_profile_without_secret(connection)
    profile_status = persist_connection_profile(profile_for_storage)
    _render_storage_status(profile_status)

    if test_clicked:
        clear_health_state(st.session_state)
        _run_connection_test(connection, profile_for_storage)
        return

    if connection_changed:
        clear_health_state(st.session_state)
        st.success(_settings_saved_message(profile_status))
        st.info(_validation_refresh_guidance(connection.auth_method))
        return

    st.success(_settings_unchanged_message(profile_status))


def _test_connection_disabled(
    existing_connection: ADMEConnection | None,
    draft_connection: ADMEConnection,
) -> bool:
    """Return True when user auth must complete before testing."""
    return (
        draft_connection.auth_method == AuthMethod.USER_IMPERSONATION
        and (
            existing_connection != draft_connection
            or get_user_auth_state(st.session_state) is None
        )
    )


def _render_user_auth_controls(connection: ADMEConnection | None) -> None:
    """Render sign-in or sign-out controls for user impersonation."""
    if (
        connection is None
        or not connection.is_valid()
        or connection.auth_method != AuthMethod.USER_IMPERSONATION
    ):
        return

    st.subheader("User sign-in")
    if get_user_auth_state(st.session_state) is not None:
        st.success("Signed in for this Streamlit session.")
        if st.button("Sign Out"):
            clear_user_auth_state(st.session_state)
            st.success("Signed out. Sign in again before testing the connection.")
        _render_paste_token_section(connection)
        return

    st.info("Sign in to enable Test Connection for this saved connection.")
    authorization_url = _authorization_url_for_user_sign_in(connection)
    if authorization_url is not None:
        st.link_button("Sign In", authorization_url, type="primary")
    _render_paste_token_section(connection)


def _render_paste_token_section(connection: ADMEConnection) -> None:
    """Render an advanced paste-a-bearer-token alternative to browser sign-in.

    For tenants where interactive sign-in is blocked (e.g. Conditional
    Access), operators can mint a token out-of-band — for example with
    ``az account get-access-token`` — and paste it here.
    """
    with st.expander("Advanced: paste a bearer token (no browser sign-in)"):
        st.caption(
            "Use this when interactive sign-in is blocked (e.g. Conditional "
            "Access). Generate a token out-of-band and paste it below. The "
            "token is held in this Streamlit session only and never written "
            "to disk."
        )
        st.code(
            "az login\n"
            "az account get-access-token "
            f'--resource "{connection.client_id}" '
            "--query accessToken -o tsv",
            language="powershell",
        )
        pasted = st.text_area(
            "Bearer token",
            key="paste_bearer_token",
            placeholder="eyJ0eXAiOiJKV1Qi...",
            help=(
                "Paste the access token value only (a long string with three "
                "dot-separated segments). It is used as-is for ADME calls."
            ),
        )
        if not st.button("Use pasted token"):
            return
        try:
            auth_state = user_auth_state_from_pasted_token(pasted)
        except AuthenticationError as exc:
            st.error(str(exc))
            return
        store_user_auth_state(st.session_state, auth_state)
        st.success(_pasted_token_summary(auth_state.access_token))
        st.rerun()


def _pasted_token_summary(token: str) -> str:
    """Return a short identity/expiry summary for a freshly accepted token."""
    identity = token_utils.extract_first_string_claim(
        token,
        ("upn", "unique_name", "preferred_username", "appid"),
    )
    expiry = token_utils.extract_expiry(token)
    parts = ["Token accepted for this session."]
    if identity:
        parts.append(f"Identity: {identity}.")
    if expiry is not None:
        local_expiry = _format_unix_local(expiry)
        parts.append(f"Expires: {local_expiry}.")
    return " ".join(parts)


def _format_unix_local(epoch_seconds: int) -> str:
    """Format a Unix timestamp as a local-time string."""
    return datetime.fromtimestamp(epoch_seconds).strftime("%Y-%m-%d %H:%M:%S")


def _authorization_url_for_user_sign_in(
    connection: ADMEConnection,
) -> str | None:
    """Return a safe authorization URL for the current pending sign-in flow."""
    pending_flow = get_pending_user_auth_flow(st.session_state)
    pending_authorization_url = _authorization_url_from_pending_flow(pending_flow)
    if pending_authorization_url:
        return pending_authorization_url

    try:
        flow_start = start_user_auth_flow(connection)
    except AuthenticationError as exc:
        st.error(str(exc))
        return None
    except Exception:  # noqa: BLE001 - avoid exposing raw auth library details
        st.error(
            "Unable to start user sign-in. Check tenant ID, client ID, and "
            "redirect URI configuration."
        )
        return None

    store_pending_user_auth_flow(st.session_state, flow_start)
    return flow_start.authorization_url


def _authorization_url_from_pending_flow(pending_flow: object) -> str | None:
    """Read the public auth URL from a stored flow without exposing its payload."""
    authorization_url = getattr(pending_flow, "authorization_url", None)
    if isinstance(authorization_url, str) and authorization_url:
        return authorization_url
    auth_url = getattr(pending_flow, "auth_url", None)
    if isinstance(auth_url, str) and auth_url:
        return auth_url
    return None


def _run_connection_test(
    connection: ADMEConnection,
    profile_for_storage: ADMEConnection | None = None,
) -> None:
    """Authenticate and validate every configured OSDU service."""
    with st.spinner("Authenticating and checking ADME services..."):
        try:
            token = _get_token_for_connection(connection)
            results = check_all(connection, token)
        except Exception as exc:  # noqa: BLE001 - present operator-facing error
            store_health_error(st.session_state, str(exc))
            st.error(
                _with_retry_guidance(
                    f"Connection test failed: {exc}",
                    connection.auth_method,
                )
            )
            return

    store_health_results(st.session_state, results)
    health_status = persist_health_run(
        profile_for_storage or connection_profile_without_secret(connection),
        results,
    )
    _render_storage_status(health_status)
    _render_validation_summary(results)


def _get_token_for_connection(connection: ADMEConnection) -> str:
    """Return an ADME token using session-scoped user auth when required."""
    if connection.auth_method == AuthMethod.USER_IMPERSONATION:
        return get_token(
            connection,
            user_auth_state=get_user_auth_state(st.session_state),
        )
    return get_token(connection)


def _render_latest_validation() -> None:
    """Render the most recent validation result for the active session."""
    results = get_health_results(st.session_state)
    error_message = get_health_error(st.session_state)
    if not results and not error_message:
        return

    st.subheader("Latest validation")
    if error_message:
        connection = get_connection(st.session_state)
        auth_method = connection.auth_method if connection is not None else None
        st.error(
            _with_retry_guidance(
                f"Last connection test failed: {error_message}",
                auth_method,
            )
        )
        return

    _render_validation_summary(results)


def _render_validation_summary(results: list[ServiceHealthResult]) -> None:
    """Render the service-by-service validation summary."""
    summary = summarize_health(results)
    if summary.overall_state == "healthy":
        st.success(
            f"All {summary.total_services} configured OSDU services responded "
            "successfully."
        )
    elif summary.overall_state == "degraded":
        st.warning(
            f"{summary.unhealthy_services} service(s) returned an unhealthy "
            "status."
        )
    else:
        st.error(
            f"{summary.error_services} service probe(s) failed before a "
            "response was returned."
        )

    st.dataframe(
        results_to_table_rows(results),
        use_container_width=True,
        hide_index=True,
    )


def _validation_refresh_guidance(auth_method: AuthMethod) -> str:
    """Return the next-step guidance after saving connection settings."""
    if auth_method == AuthMethod.USER_IMPERSONATION:
        return USER_IMPERSONATION_REFRESH_GUIDANCE
    return "Run Test Connection to refresh the service health report."


def _settings_saved_message(status: StorageSyncStatus) -> str:
    """Return confirmation copy for a changed connection profile."""
    if status.available:
        return (
            "Connection settings saved persistently. Service-principal client "
            "secret is saved in your OS credential store."
        )
    return (
        "Connection settings saved for this Streamlit session. Persistent "
        "storage was not updated."
    )


def _settings_unchanged_message(status: StorageSyncStatus) -> str:
    """Return confirmation copy for an unchanged connection profile."""
    if status.available:
        return "Connection settings are already saved persistently."
    return (
        "Connection settings are already up to date for this Streamlit session. "
        "Persistent storage was not updated."
    )


def _render_storage_status(status: StorageSyncStatus) -> None:
    """Show storage sync feedback without blocking session-only operation."""
    if not status.message:
        return
    if status.severity == "error":
        st.error(status.message)
    elif status.severity == "warning":
        st.warning(status.message)
    elif status.severity == "info":
        st.info(status.message)


def _with_retry_guidance(
    message: str,
    auth_method: AuthMethod | None = None,
) -> str:
    """Append consistent retry guidance to connection test errors."""
    if auth_method == AuthMethod.USER_IMPERSONATION:
        return f"{message} {USER_IMPERSONATION_RETRY_GUIDANCE}"
    return f"{message} {RETRY_CONNECTION_TEST_GUIDANCE}"


if __name__ == "__main__":
    main()
