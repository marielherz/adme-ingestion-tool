"""Utilities for storing ADME connection state in Streamlit session state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from app.models.connection import ADMEConnection, AuthMethod, ServiceHealthResult
from app.services import settings_store, token_utils

if TYPE_CHECKING:
    from app.services.auth import UserAuthFlowStart, UserAuthState

_IDENTITY_CLAIMS = ("upn", "unique_name", "preferred_username", "appid")
_SIGN_IN_GUIDANCE = (
    "Sign in or paste a bearer token on the Instance Configuration page."
)
_EXPIRED_GUIDANCE = (
    "Your session token expired — sign in again or paste a fresh token on the "
    "Instance Configuration page."
)
_SERVICE_PRINCIPAL_GUIDANCE = (
    "Add the service-principal client secret on the Instance Configuration page."
)

SessionStateView = Any
MutableSessionState = Any


CONNECTION_KEY = "adme_connection"
HEALTH_RESULTS_KEY = "adme_health_results"
HEALTH_ERROR_KEY = "adme_health_error"
USER_AUTH_FLOW_KEY = "adme_user_auth_flow"
USER_AUTH_STATE_KEY = "adme_user_auth_state"
DEFAULT_CONNECTION_NAME = "default"
OVERALL_STATE_LABELS = {
    "not_configured": "Not configured",
    "not_tested": "Configured · Validation pending",
    "healthy": "Healthy",
    "degraded": "Degraded",
    "error": "Validation failed",
}


@dataclass(frozen=True)
class HealthSummary:
    """Summarize the latest ADME service validation run."""

    total_services: int
    healthy_services: int
    unhealthy_services: int
    error_services: int

    @property
    def overall_state(self) -> str:
        """Return the aggregate state for the validation run."""
        if self.total_services == 0:
            return "not_tested"
        if self.error_services:
            return "error"
        if self.unhealthy_services:
            return "degraded"
        return "healthy"


def ensure_session_defaults(session_state: MutableSessionState) -> None:
    """Ensure the expected ADME session keys are always present.

    Also hydrates ``CONNECTION_KEY`` from the on-disk settings store when the
    session has no connection yet.  Failures to read disk are logged and
    swallowed at this layer because hydration is best-effort: the operator
    can always re-enter settings in the form.
    """
    session_state.setdefault(CONNECTION_KEY, None)
    session_state.setdefault(HEALTH_RESULTS_KEY, [])
    session_state.setdefault(HEALTH_ERROR_KEY, "")
    session_state.setdefault(USER_AUTH_FLOW_KEY, None)
    session_state.setdefault(USER_AUTH_STATE_KEY, None)

    if session_state.get(CONNECTION_KEY) is None:
        try:
            settings_store.initialize_store()
            active_name = settings_store.get_active_connection_name()
            if active_name:
                stored = settings_store.load_connection(active_name)
                if stored is not None:
                    session_state[CONNECTION_KEY] = stored
                    _hydrate_user_token(session_state)
        except settings_store.SettingsStoreError:
            # Hydration is additive; never block session bootstrap on disk I/O.
            pass


def get_connection(session_state: SessionStateView) -> ADMEConnection | None:
    """Return the stored connection, if one exists for this session."""
    connection = session_state.get(CONNECTION_KEY)
    if isinstance(connection, ADMEConnection):
        return connection
    return None


def save_connection(
    session_state: MutableSessionState,
    connection: ADMEConnection,
    name: str = DEFAULT_CONNECTION_NAME,
) -> None:
    """Persist the connection for the active Streamlit session and to disk.

    The durable store is updated before session state so failed persistence
    never leaves the current Streamlit session showing unsaved settings.  If
    the new connection differs from the current one, stale user auth/health
    are cleared after the durable save succeeds.
    """
    connection_changed = get_connection(session_state) != connection
    settings_store.save_connection(name, connection)
    settings_store.set_active_connection(name)

    if connection_changed:
        clear_user_auth_state(session_state)
    session_state[CONNECTION_KEY] = connection


def forget_saved_connection(
    session_state: MutableSessionState,
    name: str = DEFAULT_CONNECTION_NAME,
) -> None:
    """Remove a saved connection from disk and clear it from this session.

    Auth and health state are also cleared because they are tied to whatever
    connection just got forgotten.
    """
    settings_store.delete_connection(name)
    settings_store.clear_active_connection()
    session_state[CONNECTION_KEY] = None
    clear_user_auth_state(session_state)


def get_pending_user_auth_flow(
    session_state: SessionStateView,
) -> UserAuthFlowStart | Mapping[str, object] | None:
    """Return the pending MSAL user sign-in flow, if one is active."""
    pending_flow = session_state.get(USER_AUTH_FLOW_KEY)
    if pending_flow is None:
        return None
    return cast("UserAuthFlowStart | Mapping[str, object]", pending_flow)


def store_pending_user_auth_flow(
    session_state: MutableSessionState,
    flow: UserAuthFlowStart | Mapping[str, object],
) -> None:
    """Store an opaque pending user sign-in flow for this Streamlit session."""
    session_state[USER_AUTH_FLOW_KEY] = flow


def clear_pending_user_auth_flow(session_state: MutableSessionState) -> None:
    """Clear any pending user sign-in flow from the session."""
    session_state[USER_AUTH_FLOW_KEY] = None


def get_user_auth_state(session_state: SessionStateView) -> UserAuthState | None:
    """Return completed user auth material for backend calls, if present."""
    auth_state = session_state.get(USER_AUTH_STATE_KEY)
    if auth_state is None:
        return None
    return cast("UserAuthState", auth_state)


def store_user_auth_state(
    session_state: MutableSessionState,
    auth_state: UserAuthState,
) -> None:
    """Store completed user auth state and clear stale health results.

    The token is also persisted to the OS credential store (best-effort) so it
    survives app restarts until it expires.
    """
    if session_state.get(USER_AUTH_STATE_KEY) != auth_state:
        clear_health_state(session_state)
    session_state[USER_AUTH_STATE_KEY] = auth_state
    _persist_user_token(auth_state)


def clear_user_auth_state(session_state: MutableSessionState) -> None:
    """Clear user auth state and any health tied to that signed-in identity."""
    session_state[USER_AUTH_STATE_KEY] = None
    clear_pending_user_auth_flow(session_state)
    clear_health_state(session_state)
    _forget_user_token()


def _persist_user_token(auth_state: UserAuthState) -> None:
    """Best-effort: persist the bearer token to the OS credential store."""
    try:
        settings_store.save_user_token(
            auth_state.access_token,
            auth_state.expires_at,
        )
    except Exception:  # noqa: BLE001 - persistence is best-effort
        pass


def _forget_user_token() -> None:
    """Best-effort: remove any persisted bearer token."""
    try:
        settings_store.clear_user_token()
    except Exception:  # noqa: BLE001 - persistence is best-effort
        pass


def _hydrate_user_token(session_state: MutableSessionState) -> None:
    """Restore a persisted bearer token into the session, if one exists."""
    if session_state.get(USER_AUTH_STATE_KEY) is not None:
        return
    try:
        loaded = settings_store.load_user_token()
    except Exception:  # noqa: BLE001 - hydration is best-effort
        return
    if loaded is None:
        return
    token, expires_at = loaded
    from app.services.auth import UserAuthState as _UserAuthState  # noqa: PLC0415

    session_state[USER_AUTH_STATE_KEY] = _UserAuthState(
        access_token=token,
        expires_at=expires_at,
    )


def get_health_results(
    session_state: SessionStateView,
) -> list[ServiceHealthResult]:
    """Return the latest stored health results for the session."""
    results = session_state.get(HEALTH_RESULTS_KEY, [])
    if not isinstance(results, list):
        return []
    return [
        result for result in results if isinstance(result, ServiceHealthResult)
    ]


def get_health_error(session_state: SessionStateView) -> str:
    """Return the latest validation error message for the session."""
    error_message = session_state.get(HEALTH_ERROR_KEY, "")
    if isinstance(error_message, str):
        return error_message
    return ""


def clear_health_state(session_state: MutableSessionState) -> None:
    """Clear previously stored health results and errors."""
    session_state[HEALTH_RESULTS_KEY] = []
    session_state[HEALTH_ERROR_KEY] = ""


def store_health_results(
    session_state: MutableSessionState,
    results: Sequence[ServiceHealthResult],
) -> None:
    """Store fresh health results and clear the error banner."""
    session_state[HEALTH_RESULTS_KEY] = list(results)
    session_state[HEALTH_ERROR_KEY] = ""


def store_health_error(
    session_state: MutableSessionState,
    error_message: str,
) -> None:
    """Store the latest validation error and clear stale result rows."""
    session_state[HEALTH_RESULTS_KEY] = []
    session_state[HEALTH_ERROR_KEY] = error_message


def summarize_health(results: Sequence[ServiceHealthResult]) -> HealthSummary:
    """Return aggregate counts for a service validation run."""
    healthy_services = sum(result.status == "healthy" for result in results)
    unhealthy_services = sum(result.status == "unhealthy" for result in results)
    error_services = sum(result.status == "error" for result in results)
    return HealthSummary(
        total_services=len(results),
        healthy_services=healthy_services,
        unhealthy_services=unhealthy_services,
        error_services=error_services,
    )


def get_overall_state(session_state: SessionStateView) -> str:
    """Return the operator-facing connection state for the current session."""
    connection = get_connection(session_state)
    if connection is None or not connection.is_valid():
        return "not_configured"
    if get_health_error(session_state):
        return "error"
    results = get_health_results(session_state)
    return summarize_health(results).overall_state


@dataclass(frozen=True)
class AuthReadiness:
    """Describe whether the chosen auth method is ready to make ADME calls.

    ``ready`` gates Test Connection and per-page actions. ``identity_label`` is
    a human-friendly "acting as" description when known. ``guidance`` is the
    operator-facing next step when ``ready`` is ``False`` (empty otherwise).
    """

    ready: bool
    method: AuthMethod
    identity_label: str | None
    guidance: str


def _identity_label_from_token(token: str) -> str | None:
    """Return a friendly identity from a token's claims, if readable."""
    return token_utils.extract_first_string_claim(token, _IDENTITY_CLAIMS)


def auth_readiness(
    connection: ADMEConnection | None,
    user_auth_state: UserAuthState | None,
) -> AuthReadiness:
    """Return readiness, identity, and guidance for the connection's method.

    Service principal is ready as soon as a client secret is present (the
    ``client-id`` + secret credential authenticates without a sign-in). User
    impersonation is ready once a non-expired session token exists — obtained
    by browser sign-in *or* a pasted bearer token.
    """
    if connection is None:
        return AuthReadiness(
            ready=False,
            method=AuthMethod.USER_IMPERSONATION,
            identity_label=None,
            guidance=_SIGN_IN_GUIDANCE,
        )

    method = connection.auth_method
    if method == AuthMethod.SERVICE_PRINCIPAL:
        if connection.client_secret.strip():
            client_id = connection.client_id.strip()
            label = (
                f"service principal ({client_id})"
                if client_id
                else "service principal"
            )
            return AuthReadiness(
                ready=True,
                method=method,
                identity_label=label,
                guidance="",
            )
        return AuthReadiness(
            ready=False,
            method=method,
            identity_label=None,
            guidance=_SERVICE_PRINCIPAL_GUIDANCE,
        )

    if (
        user_auth_state is not None
        and user_auth_state.access_token
        and not user_auth_state.is_expired()
    ):
        label = (
            _identity_label_from_token(user_auth_state.access_token)
            or "signed-in user"
        )
        return AuthReadiness(
            ready=True,
            method=method,
            identity_label=label,
            guidance="",
        )

    guidance = (
        _EXPIRED_GUIDANCE
        if user_auth_state is not None and user_auth_state.is_expired()
        else _SIGN_IN_GUIDANCE
    )
    return AuthReadiness(
        ready=False,
        method=method,
        identity_label=None,
        guidance=guidance,
    )


def format_auth_method(method: AuthMethod) -> str:
    """Return a human-friendly label for an auth method."""
    if method == AuthMethod.SERVICE_PRINCIPAL:
        return "Service principal"
    return "User impersonation"


def format_overall_state(state: str) -> str:
    """Return a human-friendly label for the current connection state."""
    return OVERALL_STATE_LABELS.get(state, state.replace("_", " ").title())


def format_service_state(status: str) -> str:
    """Return a concise status label for a service probe result."""
    labels = {
        "healthy": "✅ Healthy",
        "unhealthy": "⚠️ Unhealthy",
        "error": "❌ Error",
        "unknown": "• Unknown",
    }
    return labels.get(status, status.replace("_", " ").title())


def results_to_table_rows(
    results: Sequence[ServiceHealthResult],
) -> list[dict[str, object]]:
    """Convert health results into operator-friendly table rows."""
    rows: list[dict[str, object]] = []
    for result in results:
        rows.append(
            {
                "Service": result.service_name,
                "State": format_service_state(result.status),
                "HTTP": result.status_code if result.status_code is not None else "—",
                "Latency (ms)": (
                    round(result.response_time_ms, 1)
                    if result.response_time_ms is not None
                    else "—"
                ),
                "Detail": result.error_message or "—",
            }
        )
    return rows
