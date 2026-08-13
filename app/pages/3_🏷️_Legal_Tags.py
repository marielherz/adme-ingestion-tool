"""Legal Tags management page for ADME.

CRUD UI over `/api/legal/v1/legaltags` for the connected partition: list,
view, create, edit (mutable subset only), delete (with type-the-name
confirmation), plus a properties-driven create form.  Mirrors the
entitlements + ingestion pages: pre-flight chain → autorun-once load →
sticky error pattern → history dataframe + latency line chart.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if PROJECT_ROOT not in {Path(path or ".").resolve() for path in sys.path}:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # type: ignore[import-untyped]  # noqa: E402
import streamlit as st  # type: ignore[import-not-found]  # noqa: E402

from app import services as _services_pkg  # noqa: E402
from app.connection_state import (  # noqa: E402
    auth_readiness,
    ensure_session_defaults,
    get_connection,
    get_user_auth_state,
)
from app.models.connection import (  # noqa: E402
    ADMEConnection,
    AuthMethod,
)
from app.models.osdu import (  # noqa: E402
    LegalTag,
    LegalTagDetailResult,
    LegalTagListResult,
    LegalTagOperationResult,
    LegalTagPropertiesResult,
    LegalTagPropertiesSpec,
)
from app.services.auth import AuthenticationError, get_token  # noqa: E402
from app.services.legal_tags import (  # noqa: E402
    create_legal_tag,
    delete_legal_tag,
    get_legal_tag,
    get_legal_tag_properties,
    list_legal_tags,
    update_legal_tag,
)

SETTINGS_PAGE_PATH = "pages/1_⚙️_Instance_Configuration.py"

# --- Locked session-state keys (Charlie tests these) ---------------------
AUTORUN_KEY = "legal_tags_autorun_done"
LIST_KEY = "legal_tags_list"
SELECTED_NAME_KEY = "legal_tags_selected_name"
SELECTED_DETAIL_KEY = "legal_tags_selected_detail"
EDIT_MODE_KEY = "legal_tags_edit_mode"
PROPERTIES_SPEC_KEY = "legal_tags_properties_spec"
PROPERTIES_FALLBACK_KEY = "legal_tags_properties_fallback"
LAST_ERROR_KEY = "legal_tags_last_error"
HISTORY_KEY = "legal_tags_history"
SHOW_VALID_ONLY_KEY = "legal_tags_show_valid_only"
DELETE_CONFIRM_TEXT_KEY = "legal_tags_delete_confirm_text"

# Create-form keys (one per field).
FORM_NAME_KEY = "legal_tags_create_form_name"
FORM_DESCRIPTION_KEY = "legal_tags_create_form_description"
FORM_COUNTRY_OF_ORIGIN_KEY = "legal_tags_create_form_country_of_origin"
FORM_OTHER_COUNTRIES_KEY = "legal_tags_create_form_other_countries"
FORM_CONTRACT_ID_KEY = "legal_tags_create_form_contract_id"
FORM_EXPIRATION_DATE_KEY = "legal_tags_create_form_expiration_date"
FORM_ORIGINATOR_KEY = "legal_tags_create_form_originator"
FORM_DATA_TYPE_KEY = "legal_tags_create_form_data_type"
FORM_SECURITY_KEY = "legal_tags_create_form_security"
FORM_PERSONAL_DATA_KEY = "legal_tags_create_form_personal_data"
FORM_EXPORT_CLASSIFICATION_KEY = "legal_tags_create_form_export_classification"

# Edit-form keys (mirror the create-form shape, scoped to edit mode only).
EDIT_DESCRIPTION_KEY = "legal_tags_edit_form_description"
EDIT_CONTRACT_ID_KEY = "legal_tags_edit_form_contract_id"
EDIT_EXPIRATION_DATE_KEY = "legal_tags_edit_form_expiration_date"

HISTORY_DISPLAY_LIMIT = 20

# Endpoint label shorthand for the in-session history.
LABEL_LIST = "legaltags.list"
LABEL_PROPERTIES = "legaltags.properties"


# Free-text fallback options when GET /legaltags/properties is absent
# (per Darryl's documented OSDU enums).
_FALLBACK_DATA_TYPES = [
    "Public Domain Data",
    "First Party Data",
    "Second Party Data",
    "Third Party Data",
    "Transferred Data",
]
_FALLBACK_SECURITY_CLASSIFICATIONS = ["Public", "Private", "Confidential"]
_FALLBACK_PERSONAL_DATA_TYPES = [
    "No Personal Data",
    "Personally Identifiable",
]
_FALLBACK_EXPORT_CLASSIFICATIONS = ["EAR99", "0A998"]
_COMMON_COUNTRIES = ["US", "CA", "GB", "DE", "FR", "AU", "NO", "NL", "BR"]


def main() -> None:
    """Render the Legal Tags page."""
    st.set_page_config(
        page_title="Legal Tags · ADME Control Plane",
        page_icon="🏷️",
        layout="wide",
    )
    st.title("🏷️ Legal Tags")
    st.markdown(
        "Manage legal tags for the connected ADME partition: list, view, "
        "create, edit, and delete."
    )

    ensure_session_defaults(st.session_state)
    _ensure_page_defaults()

    connection = get_connection(st.session_state)
    if not _preflight_ok(connection):
        return
    assert connection is not None  # mypy — _preflight_ok guarantees this

    st.caption(
        f"Data partition: `{connection.data_partition_id}` · "
        f"Endpoint: `{connection.endpoint}`"
    )

    _render_sticky_error()
    _render_toolbar(connection)
    _render_properties_fallback_banner()
    _maybe_autorun(connection)
    _render_main_layout(connection)
    _render_create_section(connection)
    _render_history()


# ---------------------------------------------------------------------------
# Session bootstrap
# ---------------------------------------------------------------------------


def _ensure_page_defaults() -> None:
    """Initialize page-scoped session keys."""
    st.session_state.setdefault(AUTORUN_KEY, False)
    st.session_state.setdefault(LIST_KEY, [])
    st.session_state.setdefault(SELECTED_NAME_KEY, None)
    st.session_state.setdefault(SELECTED_DETAIL_KEY, None)
    st.session_state.setdefault(EDIT_MODE_KEY, False)
    st.session_state.setdefault(PROPERTIES_SPEC_KEY, None)
    st.session_state.setdefault(PROPERTIES_FALLBACK_KEY, False)
    st.session_state.setdefault(LAST_ERROR_KEY, None)
    st.session_state.setdefault(HISTORY_KEY, [])
    st.session_state.setdefault(SHOW_VALID_ONLY_KEY, False)
    st.session_state.setdefault(DELETE_CONFIRM_TEXT_KEY, "")

    st.session_state.setdefault(FORM_NAME_KEY, "")
    st.session_state.setdefault(FORM_DESCRIPTION_KEY, "")
    st.session_state.setdefault(FORM_COUNTRY_OF_ORIGIN_KEY, [])
    st.session_state.setdefault(FORM_OTHER_COUNTRIES_KEY, [])
    st.session_state.setdefault(FORM_CONTRACT_ID_KEY, "")
    st.session_state.setdefault(
        FORM_EXPIRATION_DATE_KEY, date.today() + timedelta(days=365)
    )
    st.session_state.setdefault(FORM_ORIGINATOR_KEY, "")
    st.session_state.setdefault(FORM_DATA_TYPE_KEY, "")
    st.session_state.setdefault(FORM_SECURITY_KEY, "")
    st.session_state.setdefault(FORM_PERSONAL_DATA_KEY, "")
    st.session_state.setdefault(FORM_EXPORT_CLASSIFICATION_KEY, "")


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def _preflight_ok(connection: ADMEConnection | None) -> bool:
    """Return True when we have everything required to call the legal API."""
    if connection is None or not connection.is_valid():
        st.info(
            "No ADME connection is configured for this session. "
            "Open Instance Configuration to add your endpoint, identity details, and "
            "data partition."
        )
        st.page_link(
            SETTINGS_PAGE_PATH,
            label="Open Instance Configuration",
            icon="⚙️",
        )
        return False

    if connection.auth_method == AuthMethod.USER_IMPERSONATION:
        readiness = auth_readiness(
            connection, get_user_auth_state(st.session_state)
        )
        if not readiness.ready:
            st.info(
                f"{readiness.guidance} A token is required to manage legal tags."
            )
            st.page_link(
                SETTINGS_PAGE_PATH,
                label="Open Instance Configuration",
                icon="⚙️",
            )
            return False

    if not connection.data_partition_id.strip():
        st.info(
            "No data partition is configured for this connection. "
            "Open Instance Configuration to add the OSDU data-partition id."
        )
        st.page_link(
            SETTINGS_PAGE_PATH,
            label="Open Instance Configuration",
            icon="⚙️",
        )
        return False

    return True


def _acquire_token(connection: ADMEConnection) -> str | None:
    """Acquire an ADME token, rendering an operator-safe error on failure."""
    try:
        if connection.auth_method == AuthMethod.USER_IMPERSONATION:
            return get_token(
                connection,
                user_auth_state=get_user_auth_state(st.session_state),
            )
        return get_token(connection)
    except AuthenticationError as exc:
        st.error(
            f"Could not acquire an ADME token: {exc}. "
            "Open Instance Configuration to sign in again or update credentials."
        )
        st.page_link(
            SETTINGS_PAGE_PATH,
            label="Open Instance Configuration",
            icon="⚙️",
        )
        return None
    except Exception as exc:  # noqa: BLE001 - never expose raw auth library details
        st.error(
            f"Unexpected error acquiring an ADME token: {type(exc).__name__}. "
            "Open Instance Configuration to verify your connection."
        )
        st.page_link(
            SETTINGS_PAGE_PATH,
            label="Open Instance Configuration",
            icon="⚙️",
        )
        return None


# ---------------------------------------------------------------------------
# Sticky error
# ---------------------------------------------------------------------------


def _render_sticky_error() -> None:
    """Render the sticky error banner + dismiss button at the top of the page."""
    message = st.session_state.get(LAST_ERROR_KEY)
    if not message:
        return
    cols = st.columns([8, 1])
    with cols[0]:
        st.error(message)
    with cols[1]:
        if st.button("Dismiss error", key="legal_tags_dismiss_error"):
            st.session_state[LAST_ERROR_KEY] = None
            st.rerun()


def _set_sticky_error(message: str) -> None:
    """Persist a failure summary to the sticky-error key."""
    st.session_state[LAST_ERROR_KEY] = message


def _clear_sticky_error() -> None:
    """Clear the sticky-error key at the start of an operator action."""
    st.session_state[LAST_ERROR_KEY] = None


# ---------------------------------------------------------------------------
# Toolbar (refresh + filter)
# ---------------------------------------------------------------------------


def _render_toolbar(connection: ADMEConnection) -> None:
    """Render the Refresh button + Show-only-valid-tags toggle."""
    cols = st.columns([1, 2, 5])
    with cols[0]:
        refresh_clicked = st.button(
            "🔄 Refresh",
            key="legal_tags_refresh_button",
            help="Re-call list + properties endpoints.",
        )
    with cols[1]:
        prev_filter = bool(st.session_state.get(SHOW_VALID_ONLY_KEY, False))
        new_filter = st.toggle(
            "Show only valid tags",
            value=prev_filter,
            key=SHOW_VALID_ONLY_KEY,
            help="Calls list with ?valid=true. Filter is server-side and "
                 "may lag up to 24 h after expiration changes.",
        )
        filter_changed = bool(new_filter) != prev_filter

    if refresh_clicked or filter_changed:
        _clear_sticky_error()
        token = _acquire_token(connection)
        if token is not None:
            _refresh_list(connection, token)
            if refresh_clicked:
                # Refresh also re-pulls the property spec to pick up
                # partition-config changes; filter-toggle only re-pulls the list.
                _refresh_properties(connection, token)
            st.session_state[AUTORUN_KEY] = True
        st.rerun()


# ---------------------------------------------------------------------------
# Autorun-once
# ---------------------------------------------------------------------------


def _maybe_autorun(connection: ADMEConnection) -> None:
    """Call list + properties exactly once per session unless the toolbar bypasses."""
    if st.session_state.get(AUTORUN_KEY, False):
        return
    token = _acquire_token(connection)
    if token is None:
        # Mark autorun done so we don't retry on every rerun while the
        # operator is fixing their connection.
        st.session_state[AUTORUN_KEY] = True
        return
    _refresh_list(connection, token)
    _refresh_properties(connection, token)
    st.session_state[AUTORUN_KEY] = True


def _refresh_list(connection: ADMEConnection, token: str) -> None:
    """Call list_legal_tags with the current valid-only filter and update state."""
    valid_filter: bool | None = (
        True if st.session_state.get(SHOW_VALID_ONLY_KEY, False) else None
    )
    with st.spinner("Loading legal tags…"):
        result: LegalTagListResult = list_legal_tags(
            connection, token, valid=valid_filter
        )
    label = LABEL_LIST + (":valid" if valid_filter is True else "")
    _append_history(label, result.latency_ms, result.http_status, result.ok)
    if result.ok:
        st.session_state[LIST_KEY] = list(result.items)
    else:
        st.session_state[LIST_KEY] = []
        _set_sticky_error(
            _format_op_error("List failed", result.error_message,
                             result.http_status, result.correlation_id)
        )


def _refresh_properties(connection: ADMEConnection, token: str) -> None:
    """Call get_legal_tag_properties; on 404, set fallback flag."""
    with st.spinner("Loading legal tag properties…"):
        result: LegalTagPropertiesResult = get_legal_tag_properties(
            connection, token
        )
    _append_history(
        LABEL_PROPERTIES, result.latency_ms, result.http_status, result.ok
    )
    if result.ok and result.spec is not None:
        st.session_state[PROPERTIES_SPEC_KEY] = result.spec
        st.session_state[PROPERTIES_FALLBACK_KEY] = False
    elif result.http_status == 404:
        st.session_state[PROPERTIES_SPEC_KEY] = None
        st.session_state[PROPERTIES_FALLBACK_KEY] = True
    else:
        # Non-404 failure: keep any previous spec, mark fallback so the form
        # still works, but surface the error stickily.
        st.session_state[PROPERTIES_FALLBACK_KEY] = True
        if not result.ok:
            _set_sticky_error(
                _format_op_error(
                    "Properties endpoint failed",
                    result.error_message,
                    result.http_status,
                    result.correlation_id,
                )
            )


def _render_properties_fallback_banner() -> None:
    """Render the info banner when the properties endpoint is absent."""
    if st.session_state.get(PROPERTIES_FALLBACK_KEY, False) and (
        st.session_state.get(PROPERTIES_SPEC_KEY) is None
    ):
        st.info(
            "ℹ️ Your ADME instance does not expose legal tag property "
            "defaults. The Create form will use free-text inputs — refer "
            "to the OSDU spec for valid values."
        )


# ---------------------------------------------------------------------------
# Main layout: list + selected-tag detail
# ---------------------------------------------------------------------------


def _render_main_layout(connection: ADMEConnection) -> None:
    """Render the list dataframe (left) and selected-tag detail (right)."""
    items: list[LegalTag] = list(st.session_state.get(LIST_KEY, []))

    st.subheader(f"Existing legal tags ({len(items)})")
    if not items:
        st.caption(
            "No legal tags in this partition yet. Use the Create section "
            "below to add one."
        )
    else:
        st.dataframe(
            _list_to_table_rows(items),
            use_container_width=True,
            hide_index=True,
        )

        names = [tag.name for tag in items]
        prev_selected = st.session_state.get(SELECTED_NAME_KEY)
        # Selectbox is the reliable cross-version way to pick a row.
        options = ["—"] + names
        try:
            initial_index = (
                options.index(prev_selected) if prev_selected in options else 0
            )
        except ValueError:
            initial_index = 0
        chosen = st.selectbox(
            "Select a tag to view details",
            options=options,
            index=initial_index,
            key="legal_tags_selectbox",
        )
        new_selected = chosen if chosen != "—" else None
        if new_selected != prev_selected:
            st.session_state[SELECTED_NAME_KEY] = new_selected
            # Clear cached detail + exit edit mode when selection changes.
            st.session_state[SELECTED_DETAIL_KEY] = None
            st.session_state[EDIT_MODE_KEY] = False
            st.session_state[DELETE_CONFIRM_TEXT_KEY] = ""
            st.rerun()

    _render_detail_panel(connection)


def _list_to_table_rows(items: list[LegalTag]) -> list[dict[str, Any]]:
    """Project legal tags into stable dataframe rows."""
    rows: list[dict[str, Any]] = []
    for tag in items:
        props = tag.properties if isinstance(tag.properties, dict) else {}
        coo = props.get("countryOfOrigin", [])
        if isinstance(coo, list):
            country_str = ", ".join(str(c) for c in coo) if coo else "—"
        else:
            country_str = str(coo)
        valid_icon = (
            "✅" if tag.is_valid is True
            else "❌" if tag.is_valid is False
            else "?"
        )
        rows.append(
            {
                "name": tag.name,
                "country": country_str,
                "expiration": _str_or_dash(props.get("expirationDate")),
                "originator": _str_or_dash(props.get("originator")),
                "valid": valid_icon,
            }
        )
    return rows


def _render_detail_panel(connection: ADMEConnection) -> None:
    """Render the selected-tag detail block (read mode + Edit/Delete buttons)."""
    selected_name = st.session_state.get(SELECTED_NAME_KEY)
    if not selected_name:
        return

    # Lazy fetch detail if missing or stale.
    detail: LegalTagDetailResult | None = st.session_state.get(
        SELECTED_DETAIL_KEY
    )
    needs_fetch = detail is None or (
        detail.tag is not None and detail.tag.name != selected_name
    )
    if needs_fetch:
        token = _acquire_token(connection)
        if token is None:
            return
        detail = get_legal_tag(connection, token, selected_name)
        _append_history(
            f"legaltags.get.{selected_name}",
            detail.latency_ms,
            detail.http_status,
            detail.ok,
        )
        st.session_state[SELECTED_DETAIL_KEY] = detail
        if not detail.ok:
            _set_sticky_error(
                _format_op_error(
                    f"Get '{selected_name}' failed",
                    detail.error_message,
                    detail.http_status,
                    detail.correlation_id,
                )
            )

    st.divider()
    st.subheader(f"Selected: {selected_name}")
    if detail is None or not detail.ok or detail.tag is None:
        st.caption("Could not load details for this tag.")
        return

    tag = detail.tag
    if st.session_state.get(EDIT_MODE_KEY, False):
        _render_edit_form(connection, tag)
    else:
        _render_detail_read_mode(connection, tag, detail)


def _render_detail_read_mode(
    connection: ADMEConnection,
    tag: LegalTag,
    detail: LegalTagDetailResult,
) -> None:
    """Render the read-only detail view + action buttons."""
    props = tag.properties if isinstance(tag.properties, dict) else {}
    cols = st.columns(2)
    with cols[0]:
        st.markdown(f"**Description:** {tag.description or '_(none)_'}")
        st.markdown(
            f"**Country of origin:** "
            f"`{_join_list(props.get('countryOfOrigin'))}`"
        )
        st.markdown(
            f"**Other countries:** "
            f"`{_join_list(props.get('otherRelevantDataCountries'))}`"
        )
        st.markdown(
            f"**Contract ID:** `{_str_or_dash(props.get('contractId'))}`"
        )
        st.markdown(
            f"**Expiration date:** "
            f"`{_str_or_dash(props.get('expirationDate'))}`"
        )
    with cols[1]:
        st.markdown(
            f"**Originator:** `{_str_or_dash(props.get('originator'))}`"
        )
        st.markdown(
            f"**Data type:** `{_str_or_dash(props.get('dataType'))}`"
        )
        st.markdown(
            "**Security classification:** "
            f"`{_str_or_dash(props.get('securityClassification'))}`"
        )
        st.markdown(
            f"**Personal data:** `{_str_or_dash(props.get('personalData'))}`"
        )
        st.markdown(
            "**Export classification:** "
            f"`{_str_or_dash(props.get('exportClassification'))}`"
        )

    with st.expander("Raw response", expanded=False):
        if detail.raw_response is None:
            st.caption("No response body was returned.")
        elif isinstance(detail.raw_response, str):
            st.code(detail.raw_response, language="text")
        else:
            st.json(detail.raw_response)

    action_cols = st.columns([1, 1, 6])
    with action_cols[0]:
        if st.button(
            "✏️ Edit",
            key="legal_tags_edit_button",
            help="Edit the mutable fields (description, contract ID, "
                 "expiration date).",
        ):
            _clear_sticky_error()
            st.session_state[EDIT_MODE_KEY] = True
            # Seed the edit form with current values.
            st.session_state[EDIT_DESCRIPTION_KEY] = tag.description or ""
            st.session_state[EDIT_CONTRACT_ID_KEY] = (
                str(props.get("contractId") or "")
            )
            st.session_state[EDIT_EXPIRATION_DATE_KEY] = (
                _parse_iso_date(props.get("expirationDate"))
                or (date.today() + timedelta(days=365))
            )
            st.rerun()
    with action_cols[1]:
        # Delete confirmation lives inline (type-the-name pattern), gated below.
        delete_clicked = st.button(
            "🗑️ Delete",
            key="legal_tags_delete_button",
            help="Permanently delete this tag. Records referencing it will "
                 "be marked non-compliant on the next daily validation pass.",
        )
        if delete_clicked:
            _clear_sticky_error()
            st.session_state[DELETE_CONFIRM_TEXT_KEY] = ""
            st.session_state["_legal_tags_delete_open"] = True
            st.rerun()

    if st.session_state.get("_legal_tags_delete_open"):
        _render_delete_confirmation(connection, tag.name)


def _render_delete_confirmation(connection: ADMEConnection, name: str) -> None:
    """Render the type-the-name delete confirmation block."""
    st.warning(
        "⚠️ Deleting a legal tag is permanent. Records referencing this "
        "tag will become non-compliant. Type the tag name exactly to "
        "confirm."
    )
    typed = st.text_input(
        f"Type `{name}` to confirm",
        key=DELETE_CONFIRM_TEXT_KEY,
    )
    confirm_cols = st.columns([1, 1, 6])
    with confirm_cols[0]:
        confirm_disabled = typed.strip() != name
        if st.button(
            "Confirm delete",
            key="legal_tags_delete_confirm",
            type="primary",
            disabled=confirm_disabled,
        ):
            token = _acquire_token(connection)
            if token is not None:
                _do_delete(connection, token, name)
            st.rerun()
    with confirm_cols[1]:
        if st.button("Cancel", key="legal_tags_delete_cancel"):
            st.session_state["_legal_tags_delete_open"] = False
            st.session_state[DELETE_CONFIRM_TEXT_KEY] = ""
            st.rerun()


def _do_delete(connection: ADMEConnection, token: str, name: str) -> None:
    """Execute delete + refresh state on success / pin sticky on failure."""
    result: LegalTagOperationResult = delete_legal_tag(connection, token, name)
    _append_history(
        f"legaltags.delete.{name}",
        result.latency_ms,
        result.http_status,
        result.ok,
    )
    if result.ok:
        st.session_state[SELECTED_NAME_KEY] = None
        st.session_state[SELECTED_DETAIL_KEY] = None
        st.session_state[EDIT_MODE_KEY] = False
        st.session_state["_legal_tags_delete_open"] = False
        st.session_state[DELETE_CONFIRM_TEXT_KEY] = ""
        _refresh_list(connection, token)
        st.success(f"✅ Deleted `{name}`.")
    else:
        _set_sticky_error(
            _format_op_error(
                f"Delete '{name}' failed",
                result.error_message,
                result.http_status,
                result.correlation_id,
            )
        )


# ---------------------------------------------------------------------------
# Edit mode (mutable fields only)
# ---------------------------------------------------------------------------


def _render_edit_form(connection: ADMEConnection, tag: LegalTag) -> None:
    """Render the edit form: only mutable fields are enabled."""
    replace_mode = bool(
        getattr(_services_pkg, "LEGAL_TAGS_UPDATE_VIA_REPLACE", False)
    ) or bool(_module_flag_legal_tags("LEGAL_TAGS_UPDATE_VIA_REPLACE"))

    save_label = "💾 Save changes" if not replace_mode else "♻️ Replace tag"
    if replace_mode:
        st.warning(
            "⚠️ This ADME instance does not support direct edits. Saving "
            "will delete and recreate the tag — references in existing "
            "records may break."
        )

    props = tag.properties if isinstance(tag.properties, dict) else {}
    immutable_help = (
        "Immutable after creation. To change, delete and recreate."
    )

    with st.form("legal_tags_edit_form", clear_on_submit=False):
        # Mutable fields (editable).
        st.text_input("Name", value=tag.name, disabled=True, help=immutable_help)
        st.text_area(
            "Description",
            key=EDIT_DESCRIPTION_KEY,
            height=80,
            help="Mutable.",
        )
        st.text_input(
            "Contract ID",
            key=EDIT_CONTRACT_ID_KEY,
            help='Mutable. Use literal "No Contract Related" if no contract.',
        )
        st.date_input(
            "Expiration date",
            key=EDIT_EXPIRATION_DATE_KEY,
            help="Mutable. Must be in the future to keep the tag valid.",
        )

        # Immutable fields (read-only display).
        st.text_input(
            "Country of origin",
            value=_join_list(props.get("countryOfOrigin")),
            disabled=True,
            help=immutable_help,
        )
        st.text_input(
            "Originator",
            value=str(props.get("originator") or ""),
            disabled=True,
            help=immutable_help,
        )
        st.text_input(
            "Data type",
            value=str(props.get("dataType") or ""),
            disabled=True,
            help=immutable_help,
        )
        st.text_input(
            "Security classification",
            value=str(props.get("securityClassification") or ""),
            disabled=True,
            help=immutable_help,
        )
        st.text_input(
            "Personal data",
            value=str(props.get("personalData") or ""),
            disabled=True,
            help=immutable_help,
        )
        st.text_input(
            "Export classification",
            value=str(props.get("exportClassification") or ""),
            disabled=True,
            help=immutable_help,
        )

        cols = st.columns([1, 1, 6])
        with cols[0]:
            save_clicked = st.form_submit_button(save_label, type="primary")
        with cols[1]:
            cancel_clicked = st.form_submit_button("Cancel")

    if cancel_clicked:
        st.session_state[EDIT_MODE_KEY] = False
        st.rerun()

    if save_clicked:
        _clear_sticky_error()
        token = _acquire_token(connection)
        if token is None:
            return
        new_description = str(
            st.session_state.get(EDIT_DESCRIPTION_KEY, "")
        ).strip()
        new_contract = str(
            st.session_state.get(EDIT_CONTRACT_ID_KEY, "")
        ).strip()
        new_expiration = st.session_state.get(EDIT_EXPIRATION_DATE_KEY)
        # Build the merged properties payload (mutable fields override).
        merged_props: dict[str, Any] = dict(props)
        merged_props["contractId"] = new_contract
        if isinstance(new_expiration, date):
            merged_props["expirationDate"] = new_expiration.isoformat()

        result: LegalTagDetailResult = update_legal_tag(
            connection,
            token,
            name=tag.name,
            description=new_description,
            properties=merged_props,
        )
        _append_history(
            f"legaltags.update.{tag.name}",
            result.latency_ms,
            result.http_status,
            result.ok,
        )
        if result.ok:
            st.session_state[SELECTED_DETAIL_KEY] = result
            st.session_state[EDIT_MODE_KEY] = False
            _refresh_list(connection, token)
            st.success(f"✅ Updated `{tag.name}`.")
            st.rerun()
        else:
            _set_sticky_error(
                _format_op_error(
                    f"Update '{tag.name}' failed",
                    result.error_message,
                    result.http_status,
                    result.correlation_id,
                )
            )


def _module_flag_legal_tags(name: str) -> bool:
    """Return True when the legal_tags module exposes a truthy module-level flag."""
    try:
        from app.services import legal_tags as _lt_mod  # local to avoid cycle
    except ImportError:
        return False
    return bool(getattr(_lt_mod, name, False))


# ---------------------------------------------------------------------------
# Create section
# ---------------------------------------------------------------------------


def _render_create_section(connection: ADMEConnection) -> None:
    """Render the collapsed Create-new-tag expander with Suggest-defaults + form."""
    st.divider()
    with st.expander("➕ Create new legal tag", expanded=False):
        if st.button(
            "🪄 Suggest defaults",
            key="legal_tags_suggest_defaults",
            help="Fill the form with first-time-operator defaults derived "
                 "from this partition.",
        ):
            _populate_create_defaults(connection)
            st.rerun()

        spec: LegalTagPropertiesSpec | None = st.session_state.get(
            PROPERTIES_SPEC_KEY
        )
        fallback = bool(st.session_state.get(PROPERTIES_FALLBACK_KEY, False))

        country_options = _options_or_fallback(
            spec.country_of_origin if spec else None, _COMMON_COUNTRIES
        )
        other_country_options = _options_or_fallback(
            spec.other_relevant_data_countries if spec else None,
            _COMMON_COUNTRIES,
        )
        data_type_options = _options_or_fallback(
            spec.data_types if spec else None, _FALLBACK_DATA_TYPES
        )
        security_options = _options_or_fallback(
            spec.security_classifications if spec else None,
            _FALLBACK_SECURITY_CLASSIFICATIONS,
        )
        personal_options = _options_or_fallback(
            spec.personal_data_types if spec else None,
            _FALLBACK_PERSONAL_DATA_TYPES,
        )
        export_options = _options_or_fallback(
            spec.export_classifications if spec else None,
            _FALLBACK_EXPORT_CLASSIFICATIONS,
        )

        st.text_input(
            "Name",
            key=FORM_NAME_KEY,
            placeholder=f"e.g. {connection.data_partition_id}-public-data",
            help=f"Will be auto-prefixed with `{connection.data_partition_id}-`"
                 " if missing.",
        )
        st.text_area(
            "Description",
            key=FORM_DESCRIPTION_KEY,
            height=80,
            help="Free text describing the tag's purpose. Mutable later.",
        )

        # Country dropdowns: multiselect when spec available, free-text fallback.
        if fallback and spec is None:
            _country_text_fallback(
                FORM_COUNTRY_OF_ORIGIN_KEY,
                "Country of origin (ISO Alpha-2, comma-separated)",
                "e.g. US, CA",
                "ISO 3166-1 alpha-2 codes (NOT alpha-3). Required.",
            )
            _country_text_fallback(
                FORM_OTHER_COUNTRIES_KEY,
                "Other relevant data countries (optional)",
                "e.g. GB, FR",
                "ISO 3166-1 alpha-2 codes (NOT alpha-3). Optional.",
            )
        else:
            st.multiselect(
                "Country of origin",
                options=country_options,
                key=FORM_COUNTRY_OF_ORIGIN_KEY,
                help="ISO 3166-1 alpha-2 codes. Required.",
            )
            st.multiselect(
                "Other relevant data countries (optional)",
                options=other_country_options,
                key=FORM_OTHER_COUNTRIES_KEY,
                help="ISO 3166-1 alpha-2 codes. Optional.",
            )

        st.text_input(
            "Contract ID",
            key=FORM_CONTRACT_ID_KEY,
            placeholder="e.g. No Contract Related",
            help='Use literal "No Contract Related" when no contract applies.',
        )
        st.date_input(
            "Expiration date",
            key=FORM_EXPIRATION_DATE_KEY,
            help="Must be in the future. Defaults to today + 1 year.",
        )
        st.text_input(
            "Originator",
            key=FORM_ORIGINATOR_KEY,
            placeholder="e.g. ADME Operator",
            help="Free text — name of the client or supplier. Required.",
        )

        _render_create_select(
            "Data type", FORM_DATA_TYPE_KEY, data_type_options, fallback,
            spec_present=spec is not None,
            help_text="Required. Pulled from /legaltags/properties.",
        )
        _render_create_select(
            "Security classification", FORM_SECURITY_KEY, security_options,
            fallback, spec_present=spec is not None,
            help_text="Required. ADME does not allow 'Secret'.",
        )
        _render_create_select(
            "Personal data", FORM_PERSONAL_DATA_KEY, personal_options,
            fallback, spec_present=spec is not None,
            help_text="Required. ADME does not allow Sensitive PII.",
        )
        _render_create_select(
            "Export classification", FORM_EXPORT_CLASSIFICATION_KEY,
            export_options, fallback, spec_present=spec is not None,
            help_text="Required. e.g. EAR99.",
        )

        # Pre-form validation gate (lists missing fields).
        missing = _collect_missing_create_fields()
        if missing:
            bullets = "\n".join(f"- {field}" for field in missing)
            st.warning(f"Fill in before creating:\n{bullets}")

        if st.button(
            "✅ Create",
            key="legal_tags_create_button",
            type="primary",
            disabled=bool(missing),
        ):
            _do_create(connection)


def _render_create_select(
    label: str,
    key: str,
    options: list[str],
    fallback: bool,
    *,
    spec_present: bool,
    help_text: str,
) -> None:
    """Render a selectbox when options are available, free-text otherwise."""
    if fallback and not spec_present and not options:
        st.text_input(label, key=key, help=help_text)
        return
    current = st.session_state.get(key, "")
    options_with_blank = ["—"] + options
    initial_index = (
        options_with_blank.index(current) if current in options_with_blank
        else 0
    )
    chosen = st.selectbox(
        label,
        options=options_with_blank,
        index=initial_index,
        help=help_text,
        key=f"{key}__widget",
    )
    st.session_state[key] = "" if chosen == "—" else chosen


def _country_text_fallback(
    key: str,
    label: str,
    placeholder: str,
    help_text: str,
) -> None:
    """Render a comma-separated text input that round-trips to list[str]."""
    current = st.session_state.get(key, [])
    if isinstance(current, list):
        current_str = ", ".join(str(v) for v in current)
    else:
        current_str = str(current)
    typed = st.text_input(
        label,
        value=current_str,
        placeholder=placeholder,
        help=help_text,
        key=f"{key}__text",
    )
    parsed = [p.strip() for p in typed.split(",") if p.strip()]
    st.session_state[key] = parsed


def _options_or_fallback(
    options: list[str] | None,
    fallback: list[str],
) -> list[str]:
    """Return server-provided options when present, else fallback list."""
    if options:
        return list(options)
    return list(fallback)


def _collect_missing_create_fields() -> list[str]:
    """Return human-readable names of empty required create-form fields."""
    missing: list[str] = []
    if not str(st.session_state.get(FORM_NAME_KEY, "")).strip():
        missing.append("Name")
    if not str(st.session_state.get(FORM_DESCRIPTION_KEY, "")).strip():
        missing.append("Description")
    if not list(st.session_state.get(FORM_COUNTRY_OF_ORIGIN_KEY, []) or []):
        missing.append("Country of origin (at least one)")
    if not str(st.session_state.get(FORM_CONTRACT_ID_KEY, "")).strip():
        missing.append("Contract ID")
    if not isinstance(st.session_state.get(FORM_EXPIRATION_DATE_KEY), date):
        missing.append("Expiration date")
    if not str(st.session_state.get(FORM_ORIGINATOR_KEY, "")).strip():
        missing.append("Originator")
    if not str(st.session_state.get(FORM_DATA_TYPE_KEY, "")).strip():
        missing.append("Data type")
    if not str(st.session_state.get(FORM_SECURITY_KEY, "")).strip():
        missing.append("Security classification")
    if not str(st.session_state.get(FORM_PERSONAL_DATA_KEY, "")).strip():
        missing.append("Personal data")
    if not str(
        st.session_state.get(FORM_EXPORT_CLASSIFICATION_KEY, "")
    ).strip():
        missing.append("Export classification")
    return missing


def _populate_create_defaults(connection: ADMEConnection) -> None:
    """Fill the create-form session keys with TNO-loader-style defaults."""
    partition = connection.data_partition_id.strip() or "default"
    spec: LegalTagPropertiesSpec | None = st.session_state.get(
        PROPERTIES_SPEC_KEY
    )
    st.session_state[FORM_NAME_KEY] = f"{partition}-default-legal-tag"
    st.session_state[FORM_DESCRIPTION_KEY] = (
        "Default legal tag for ADME ingestion via the control plane."
    )
    st.session_state[FORM_COUNTRY_OF_ORIGIN_KEY] = ["US"]
    st.session_state[FORM_OTHER_COUNTRIES_KEY] = []
    st.session_state[FORM_CONTRACT_ID_KEY] = "No Contract Related"
    st.session_state[FORM_EXPIRATION_DATE_KEY] = date(2099, 12, 31)
    st.session_state[FORM_ORIGINATOR_KEY] = "ADME Operator"
    st.session_state[FORM_DATA_TYPE_KEY] = _pick_default(
        spec.data_types if spec else None,
        _FALLBACK_DATA_TYPES,
        preferred="Public Domain Data",
    )
    st.session_state[FORM_SECURITY_KEY] = _pick_default(
        spec.security_classifications if spec else None,
        _FALLBACK_SECURITY_CLASSIFICATIONS,
        preferred="Public",
    )
    st.session_state[FORM_PERSONAL_DATA_KEY] = _pick_default(
        spec.personal_data_types if spec else None,
        _FALLBACK_PERSONAL_DATA_TYPES,
        preferred="No Personal Data",
    )
    st.session_state[FORM_EXPORT_CLASSIFICATION_KEY] = _pick_default(
        spec.export_classifications if spec else None,
        _FALLBACK_EXPORT_CLASSIFICATIONS,
        preferred="EAR99",
    )


def _pick_default(
    options: list[str] | None,
    fallback: list[str],
    *,
    preferred: str,
) -> str:
    """Return the preferred value if present in options/fallback, else first."""
    pool = list(options) if options else list(fallback)
    if preferred in pool:
        return preferred
    return pool[0] if pool else preferred


def _do_create(connection: ADMEConnection) -> None:
    """Build the create payload + call create_legal_tag + refresh state."""
    _clear_sticky_error()
    token = _acquire_token(connection)
    if token is None:
        return

    partition = connection.data_partition_id.strip()
    raw_name = str(st.session_state.get(FORM_NAME_KEY, "")).strip()
    if partition and not raw_name.startswith(f"{partition}-"):
        final_name = f"{partition}-{raw_name}"
    else:
        final_name = raw_name

    description = str(
        st.session_state.get(FORM_DESCRIPTION_KEY, "")
    ).strip()
    expiration = st.session_state.get(FORM_EXPIRATION_DATE_KEY)
    expiration_str = (
        expiration.isoformat() if isinstance(expiration, date) else ""
    )

    properties: dict[str, Any] = {
        "countryOfOrigin": list(
            st.session_state.get(FORM_COUNTRY_OF_ORIGIN_KEY, []) or []
        ),
        "contractId": str(
            st.session_state.get(FORM_CONTRACT_ID_KEY, "")
        ).strip(),
        "expirationDate": expiration_str,
        "originator": str(
            st.session_state.get(FORM_ORIGINATOR_KEY, "")
        ).strip(),
        "dataType": str(
            st.session_state.get(FORM_DATA_TYPE_KEY, "")
        ).strip(),
        "securityClassification": str(
            st.session_state.get(FORM_SECURITY_KEY, "")
        ).strip(),
        "personalData": str(
            st.session_state.get(FORM_PERSONAL_DATA_KEY, "")
        ).strip(),
        "exportClassification": str(
            st.session_state.get(FORM_EXPORT_CLASSIFICATION_KEY, "")
        ).strip(),
    }
    other_countries = list(
        st.session_state.get(FORM_OTHER_COUNTRIES_KEY, []) or []
    )
    if other_countries:
        properties["otherRelevantDataCountries"] = other_countries

    result: LegalTagDetailResult = create_legal_tag(
        connection,
        token,
        name=final_name,
        description=description,
        properties=properties,
    )
    _append_history(
        f"legaltags.create.{final_name}",
        result.latency_ms,
        result.http_status,
        result.ok,
    )
    if result.ok and result.tag is not None:
        # Server may have re-prefixed the name; trust the server response.
        canonical = result.tag.name
        st.success(f"✅ Created `{canonical}`.")
        # Reset the create form so the operator sees a clean slate.
        _reset_create_form()
        st.session_state[SELECTED_NAME_KEY] = canonical
        st.session_state[SELECTED_DETAIL_KEY] = result
        _refresh_list(connection, token)
        st.rerun()
    else:
        _set_sticky_error(
            _format_op_error(
                f"Create '{final_name}' failed",
                result.error_message,
                result.http_status,
                result.correlation_id,
            )
        )


def _reset_create_form() -> None:
    """Reset all create-form session keys to their defaults."""
    st.session_state[FORM_NAME_KEY] = ""
    st.session_state[FORM_DESCRIPTION_KEY] = ""
    st.session_state[FORM_COUNTRY_OF_ORIGIN_KEY] = []
    st.session_state[FORM_OTHER_COUNTRIES_KEY] = []
    st.session_state[FORM_CONTRACT_ID_KEY] = ""
    st.session_state[FORM_EXPIRATION_DATE_KEY] = (
        date.today() + timedelta(days=365)
    )
    st.session_state[FORM_ORIGINATOR_KEY] = ""
    st.session_state[FORM_DATA_TYPE_KEY] = ""
    st.session_state[FORM_SECURITY_KEY] = ""
    st.session_state[FORM_PERSONAL_DATA_KEY] = ""
    st.session_state[FORM_EXPORT_CLASSIFICATION_KEY] = ""


# ---------------------------------------------------------------------------
# History panel
# ---------------------------------------------------------------------------


def _append_history(
    endpoint: str,
    latency_ms: float,
    http_status: int | None,
    ok: bool,
) -> None:
    """Append one history entry per API call."""
    history: list[dict[str, Any]] = list(
        st.session_state.get(HISTORY_KEY, [])
    )
    history.append(
        {
            "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endpoint": endpoint,
            "latency_ms": round(float(latency_ms), 1),
            "http_status": http_status,
            "ok": ok,
        }
    )
    st.session_state[HISTORY_KEY] = history


def _render_history() -> None:
    """Render latency chart, history table, and clear button."""
    history: list[dict[str, Any]] = list(
        st.session_state.get(HISTORY_KEY, [])
    )
    st.divider()
    st.subheader(f"History ({len(history)})")

    if not history:
        st.caption("No legal-tag API calls yet this session.")
        return

    if st.button("🧹 Clear history", key="legal_tags_clear_history"):
        st.session_state[HISTORY_KEY] = []
        st.rerun()

    chart_df = _history_to_chart_frame(history)
    if not chart_df.empty:
        st.line_chart(chart_df, y_label="latency (ms)")

    st.dataframe(
        _history_to_table_rows(history),
        use_container_width=True,
        hide_index=True,
    )


def _history_to_chart_frame(history: list[dict[str, Any]]) -> pd.DataFrame:
    """Pivot history into a timestamp-indexed frame with one col per endpoint."""
    if not history:
        return pd.DataFrame()
    frame = pd.DataFrame(history)
    pivoted = frame.pivot_table(
        index="timestamp",
        columns="endpoint",
        values="latency_ms",
        aggfunc="last",
    )
    return pivoted.sort_index()


def _history_to_table_rows(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project history into newest-first rows for st.dataframe."""
    recent = list(reversed(history))[:HISTORY_DISPLAY_LIMIT]
    rows: list[dict[str, Any]] = []
    for entry in recent:
        rows.append(
            {
                "timestamp": entry.get("timestamp", ""),
                "endpoint": entry.get("endpoint", ""),
                "latency_ms": f"{float(entry.get('latency_ms', 0.0)):.1f}",
                "http_status": (
                    entry.get("http_status")
                    if entry.get("http_status") is not None
                    else "—"
                ),
                "ok": "✅" if entry.get("ok") else "❌",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _format_op_error(
    headline: str,
    message: str | None,
    http_status: int | None,
    correlation_id: str | None,
) -> str:
    """Format a single-line operator-facing failure summary."""
    body = message or "Unknown error."
    status_part = (
        f"HTTP {http_status}" if http_status is not None else "no HTTP response"
    )
    correlation_part = (
        f"correlation `{correlation_id}`" if correlation_id else "no correlation id"
    )
    return f"❌ {headline}: {body} ({status_part} · {correlation_part})"


def _str_or_dash(value: Any) -> str:
    """Return str(value) or `—` for blank/None."""
    if value is None:
        return "—"
    text = str(value)
    return text if text.strip() else "—"


def _join_list(value: Any) -> str:
    """Render a list as comma-joined; non-list values fall through."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "—"
    if value in (None, ""):
        return "—"
    return str(value)


def _parse_iso_date(value: Any) -> date | None:
    """Parse a `YYYY-MM-DD` string into a date; return None on failure."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


if __name__ == "__main__":
    main()
