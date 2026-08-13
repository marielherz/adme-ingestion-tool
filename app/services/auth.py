"""Authentication helpers for Azure Data Manager for Energy connections."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

import msal  # type: ignore[import-untyped]
from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.identity import (
    ClientSecretCredential,
    CredentialUnavailableError,
)

from app.models.connection import ADMEConnection, AuthMethod
from app.services import token_utils

USER_AUTH_REDIRECT_URI = "http://localhost:8501"
MSAL_AUTHORITY_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}"
_TOKEN_EXPIRY_SKEW_SECONDS = 60


class _MsalPublicClient(Protocol):
    def initiate_auth_code_flow(
        self,
        scopes: list[str],
        redirect_uri: str,
    ) -> dict[str, object]:
        ...

    def acquire_token_by_auth_code_flow(
        self,
        auth_code_flow: dict[str, object],
        auth_response: Mapping[str, str],
    ) -> dict[str, object]:
        ...


@dataclass(frozen=True)
class UserAuthFlowStart:
    """MSAL user sign-in start result.

    The flow payload is intentionally opaque and hidden from repr because it
    contains PKCE/state material that must stay in Streamlit session state only.
    """

    authorization_url: str = field(repr=False)
    flow: Mapping[str, object] = field(repr=False)

    @property
    def auth_url(self) -> str:
        """Compatibility alias for UI callers that prefer a shorter name."""
        return self.authorization_url


@dataclass(frozen=True)
class UserAuthState:
    """Session-scoped user auth material for ADME requests."""

    access_token: str = field(repr=False)
    expires_at: int | None = None

    def is_expired(self) -> bool:
        """Return True when the token is at or near expiry."""
        if self.expires_at is None:
            return False
        return self.expires_at <= int(time.time()) + _TOKEN_EXPIRY_SKEW_SECONDS


class AuthenticationError(RuntimeError):
    """Raised when an ADME access token cannot be acquired."""


def start_user_auth_flow(connection: ADMEConnection) -> UserAuthFlowStart:
    """Start an app-managed MSAL authorization-code + PKCE user flow."""
    _validate_connection(connection)
    auth_method = _normalize_auth_method(connection)
    if auth_method != AuthMethod.USER_IMPERSONATION:
        raise AuthenticationError(
            "User sign-in flow can only be started for user impersonation "
            "connections."
        )

    app = _build_msal_app(connection)
    try:
        flow = app.initiate_auth_code_flow(
            scopes=[connection.scope],
            redirect_uri=USER_AUTH_REDIRECT_URI,
        )
    except ValueError:
        raise AuthenticationError(
            "Unable to start user sign-in. Check tenant ID, client ID, and "
            "redirect URI configuration."
        ) from None
    if "error" in flow:
        raise AuthenticationError(
            _format_msal_error("Unable to start user sign-in", flow)
        )

    authorization_url = flow.get("auth_uri")
    if not isinstance(authorization_url, str) or not authorization_url:
        raise AuthenticationError(
            "MSAL did not return an authorization URL. Start sign-in again."
        )

    return UserAuthFlowStart(authorization_url=authorization_url, flow=flow)


def complete_user_auth_flow(
    connection: ADMEConnection,
    flow: Mapping[str, object] | UserAuthFlowStart,
    callback_params: Mapping[str, object],
) -> UserAuthState:
    """Complete an MSAL authorization-code + PKCE flow from callback params."""
    _validate_connection(connection)
    auth_method = _normalize_auth_method(connection)
    if auth_method != AuthMethod.USER_IMPERSONATION:
        raise AuthenticationError(
            "User sign-in flow can only be completed for user impersonation "
            "connections."
        )

    pending_flow = _extract_pending_flow(flow)
    normalized_callback_params = _normalize_callback_params(callback_params)

    if "error" in normalized_callback_params:
        raise AuthenticationError(
            _format_oauth_callback_error(normalized_callback_params)
        )
    if "state" not in normalized_callback_params:
        raise AuthenticationError(
            "User sign-in callback is missing state. Start sign-in again."
        )
    if "code" not in normalized_callback_params:
        raise AuthenticationError(
            "User sign-in callback is missing an authorization code. "
            "Start sign-in again."
        )

    app = _build_msal_app(connection)
    try:
        result = app.acquire_token_by_auth_code_flow(
            auth_code_flow=dict(pending_flow),
            auth_response=normalized_callback_params,
        )
    except ValueError:
        raise AuthenticationError(
            "User sign-in callback did not match the pending authentication "
            "flow. Start sign-in again."
        ) from None

    return _user_auth_state_from_msal_result(result)


def user_auth_state_from_pasted_token(token: str) -> UserAuthState:
    """Build user auth state from a manually pasted bearer token.

    Intended for environments where interactive browser sign-in is blocked
    (for example Microsoft Entra Conditional Access) but the operator can
    mint a token out-of-band, such as with
    ``az account get-access-token --resource <adme-app-id>``.

    The token is a bearer credential we send to ADME as-is; we only decode it
    locally to read its expiry so the session can warn before it lapses. No
    signature, issuer, or audience validation happens here — the trust
    boundary is whoever issued the token (see ``token_utils``).
    """
    cleaned = token.strip()
    if not cleaned:
        raise AuthenticationError("Paste a non-empty bearer token.")
    if cleaned.count(".") != 2:
        raise AuthenticationError(
            "That does not look like a JWT bearer token (expected three "
            "dot-separated segments). Re-copy the full token value."
        )
    expires_at = token_utils.extract_expiry(cleaned)
    if expires_at is None:
        raise AuthenticationError(
            "Could not read an expiry from the pasted token. Re-copy the full "
            "token value (for example from 'az account get-access-token')."
        )
    state = UserAuthState(access_token=cleaned, expires_at=expires_at)
    if state.is_expired():
        raise AuthenticationError(
            "The pasted token is already expired. Generate a fresh token and "
            "paste it again."
        )
    return state


def get_token(
    connection: ADMEConnection,
    user_auth_state: UserAuthState | None = None,
) -> str:
    """Acquire and return an OAuth access token for the given connection."""
    _validate_connection(connection)

    auth_method = _normalize_auth_method(connection)
    if auth_method == AuthMethod.USER_IMPERSONATION:
        return _get_user_impersonation_token(user_auth_state)

    credential: ClientSecretCredential | None = None
    try:
        credential = _build_service_principal_credential(connection)
        access_token = credential.get_token(connection.scope)
    except CredentialUnavailableError as exc:
        raise AuthenticationError(
            _format_service_principal_error(
                auth_method,
                "credential is unavailable",
                exc,
            )
        ) from exc
    except ClientAuthenticationError as exc:
        raise AuthenticationError(
            _format_service_principal_error(
                auth_method,
                "authentication failed",
                exc,
            )
        ) from exc
    except AzureError as exc:
        raise AuthenticationError(
            _format_service_principal_error(
                auth_method,
                "token acquisition failed",
                exc,
            )
        ) from exc
    finally:
        _close_credential(credential)

    if not access_token.token:
        raise AuthenticationError("Azure AD returned an empty access token.")

    return access_token.token


_CLI_TOKEN_TIMEOUT_SECONDS = 30
_REFRESH_SKEW_SECONDS = 300


def acquire_cli_token(
    resource: str | None = None, *, timeout: int = _CLI_TOKEN_TIMEOUT_SECONDS
) -> str:
    """Return a fresh access token from the Azure CLI (``az``).

    Mirrors ``az account get-access-token --query accessToken -o tsv`` (the
    same command an operator runs by hand), so a long-running load can keep
    minting valid tokens without manual re-pasting. ``az`` must be installed
    and signed in (``az login``). Raises :class:`AuthenticationError` on any
    failure. Args are passed as a list (no shell) so ``resource`` cannot
    inject a command.
    """
    az_path = shutil.which("az")
    if az_path is None:
        raise AuthenticationError(
            "Azure CLI ('az') was not found on PATH. Install it and run "
            "'az login' to enable CLI token auto-refresh."
        )
    args = [
        az_path,
        "account",
        "get-access-token",
        "--query",
        "accessToken",
        "-o",
        "tsv",
    ]
    if resource:
        args += ["--resource", resource]
    try:
        result = subprocess.run(  # noqa: S603 - fixed args, no shell
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AuthenticationError(
            f"Azure CLI token request timed out after {timeout}s."
        ) from exc
    except OSError as exc:
        raise AuthenticationError(
            f"Could not run the Azure CLI: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise AuthenticationError(
            "Azure CLI token request failed. Run 'az login' and try again."
            + (f" Details: {detail[:200]}" if detail else "")
        )
    token = (result.stdout or "").strip()
    if not token:
        raise AuthenticationError("Azure CLI returned an empty token.")
    return token


class RefreshingTokenProvider:
    """A callable that returns a valid token, refreshing near expiry.

    Wraps an ``acquire`` callable (e.g. :func:`acquire_cli_token`) and caches
    the token until it is missing or within ``skew_seconds`` of its ``exp``
    claim, at which point it re-acquires. Designed to be passed to the bulk
    loaders so a load that outlives a single token keeps going.
    """

    def __init__(
        self,
        acquire: Callable[[], str],
        *,
        skew_seconds: int = _REFRESH_SKEW_SECONDS,
    ) -> None:
        self._acquire = acquire
        self._skew = skew_seconds
        self._token: str | None = None
        self._expires_at: float = 0.0

    def __call__(self) -> str:
        now = time.time()
        if self._token is None or now >= self._expires_at - self._skew:
            self._token = self._acquire()
            exp = token_utils.extract_expiry(self._token)
            # Fall back to a conservative ~50 min lifetime when exp is absent.
            self._expires_at = float(exp) if exp else now + 3000
        return self._token


def _build_msal_app(connection: ADMEConnection) -> _MsalPublicClient:
    try:
        return cast(
            _MsalPublicClient,
            msal.PublicClientApplication(
                client_id=connection.client_id,
                authority=MSAL_AUTHORITY_TEMPLATE.format(
                    tenant_id=connection.tenant_id
                ),
            ),
        )
    except ValueError:
        raise AuthenticationError(
            "MSAL public client could not be initialized. Check tenant ID and "
            "client ID."
        ) from None


def _build_service_principal_credential(
    connection: ADMEConnection,
) -> ClientSecretCredential:
    return ClientSecretCredential(
        tenant_id=connection.tenant_id,
        client_id=connection.client_id,
        client_secret=connection.client_secret,
    )


def _validate_connection(connection: ADMEConnection) -> None:
    if not connection.is_valid():
        raise ValueError(
            "ADME connection is incomplete. Endpoint, tenant ID, client ID, and "
            "data partition ID are required. Service principal auth also requires "
            "a client secret."
        )


def _normalize_auth_method(connection: ADMEConnection) -> AuthMethod:
    try:
        return AuthMethod(connection.auth_method)
    except ValueError as exc:
        raise AuthenticationError(
            f"Unsupported authentication method: {connection.auth_method!r}."
        ) from exc


def _format_service_principal_error(
    auth_method: AuthMethod,
    failure: str,
    exc: Exception,
) -> str:
    return f"{auth_method.value} {failure}: {exc}"


def _get_user_impersonation_token(user_auth_state: UserAuthState | None) -> str:
    if user_auth_state is None:
        raise AuthenticationError(
            "User sign-in is required before requesting an ADME access token. "
            "Start Sign In and complete the browser callback."
        )
    if user_auth_state.is_expired():
        raise AuthenticationError("User sign-in has expired. Sign in again.")
    if not user_auth_state.access_token:
        raise AuthenticationError(
            "User sign-in state does not contain an access token. Sign in again."
        )

    return user_auth_state.access_token


def _extract_pending_flow(
    flow: Mapping[str, object] | UserAuthFlowStart,
) -> Mapping[str, object]:
    if isinstance(flow, UserAuthFlowStart):
        return flow.flow
    if not flow:
        raise AuthenticationError("Missing pending user sign-in flow. Sign in again.")
    return flow


def _normalize_callback_params(callback_params: Mapping[str, object]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in callback_params.items():
        if value is None:
            continue
        if isinstance(value, str):
            normalized[key] = value
            continue
        if isinstance(value, Sequence) and not isinstance(
            value,
            (bytes, bytearray),
        ):
            if not value:
                continue
            normalized[key] = str(value[0])
            continue
        normalized[key] = str(value)
    return normalized


def _user_auth_state_from_msal_result(result: Mapping[str, object]) -> UserAuthState:
    if "error" in result:
        raise AuthenticationError(
            _format_msal_error("User sign-in token exchange failed", result)
        )

    access_token = result.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthenticationError(
            "User sign-in token exchange completed without an access token. "
            "Sign in again."
        )

    return UserAuthState(
        access_token=access_token,
        expires_at=_expires_at(result.get("expires_in")),
    )


def _expires_at(expires_in: object) -> int | None:
    if isinstance(expires_in, bool):
        return None
    if isinstance(expires_in, int | float):
        seconds = int(expires_in)
    elif isinstance(expires_in, str) and expires_in.isdecimal():
        seconds = int(expires_in)
    else:
        return None

    return int(time.time()) + seconds


def _format_oauth_callback_error(callback_params: Mapping[str, str]) -> str:
    error_code = _safe_error_code(callback_params.get("error"))
    return f"User sign-in failed ({error_code}). Start sign-in again."


def _format_msal_error(prefix: str, result: Mapping[str, object]) -> str:
    error_code = _safe_error_code(result.get("error"))
    return f"{prefix} ({error_code}). Start sign-in again."


def _safe_error_code(value: object) -> str:
    if not isinstance(value, str):
        return "unknown_error"
    allowed = {"_", "-", "."}
    sanitized = "".join(
        character for character in value if character.isalnum() or character in allowed
    )
    return sanitized or "unknown_error"


def _close_credential(credential: ClientSecretCredential | None) -> None:
    if credential is None:
        return

    close_credential = getattr(credential, "close", None)
    if callable(close_credential):
        close_credential()
