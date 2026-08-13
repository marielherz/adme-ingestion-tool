"""Entry point for the ADME control plane Streamlit app.

Uses ``st.navigation`` to group pages into a Setup / Ingest / Operate
hierarchy that mirrors the operator's journey: configure once (Setup),
load data via one of several methods (Ingest), then work with it (Operate).
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if PROJECT_ROOT not in {Path(path or ".").resolve() for path in sys.path}:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # type: ignore[import-not-found]  # noqa: E402

from app.connection_state import (  # noqa: E402
    ensure_session_defaults,
    format_auth_method,
    format_overall_state,
    get_connection,
    get_health_error,
    get_health_results,
    get_overall_state,
    results_to_table_rows,
    summarize_health,
)
from app.storage_bridge import (  # noqa: E402
    StorageSyncStatus,
    load_persisted_connection_state,
)

INSTANCE_CONFIG_PAGE_PATH = "pages/1_⚙️_Instance_Configuration.py"
ENTITLEMENTS_PAGE_PATH = "pages/2_🔑_Entitlements.py"
LEGAL_TAGS_PAGE_PATH = "pages/3_🏷️_Legal_Tags.py"
INGEST_PAGE_PATH = "pages/4_📥_Ingest.py"
INGESTION_PAGE_PATH = "pages/5_📄_Manifest.py"
FILE_UPLOAD_PAGE_PATH = "pages/6_📂_File.py"
VOLVE_PAGE_PATH = "pages/7_🌊_Volve_Dataset.py"
SEARCH_PAGE_PATH = "pages/8_🔍_Search.py"
BULK_LOAD_PAGE_PATH = "pages/9_📥_Bulk_Load.py"
TNO_PAGE_PATH = "pages/11_📦_TNO_Dataset.py"
HISTORY_PAGE_PATH = "pages/10_📊_History.py"


def _render_home() -> None:
    """Render the home / welcome view (session connection status)."""
    st.title("ADME Control Plane")
    st.markdown(
        "Connect an Azure Data Manager for Energy instance, validate core "
        "OSDU services, and keep operators on the shortest path to action."
    )
    st.caption(
        "Saved connection profiles and completed validation results load from "
        "persistent storage when available. Service-principal secrets are "
        "stored in the OS credential store; Microsoft sign-in remains tied to "
        "each Streamlit session."
    )
    st.page_link(
        INSTANCE_CONFIG_PAGE_PATH,
        label="Open Instance Configuration",
        icon="⚙️",
    )
    st.page_link(
        ENTITLEMENTS_PAGE_PATH,
        label="Open Entitlements smoke test",
        icon="🔑",
    )

    ensure_session_defaults(st.session_state)
    _render_storage_status(load_persisted_connection_state(st.session_state))
    connection = get_connection(st.session_state)
    overall_state = get_overall_state(st.session_state)

    st.subheader("Connection state")
    st.markdown(f"**Status:** {format_overall_state(overall_state)}")
    st.caption(
        "Need to change this session's connection, restore a client secret, or "
        "sign in again? Open Instance Configuration."
    )

    if connection is None or overall_state == "not_configured":
        st.warning("No ADME connection is configured for this session.")
        st.info(
            "Go to Instance Configuration to add your ADME endpoint, "
            "identity details, data partition, and validation workflow."
        )
        return

    st.subheader("Current session connection")
    st.markdown(
        "\n".join(
            [
                f"- **Endpoint:** `{connection.endpoint}`",
                f"- **Data partition:** `{connection.data_partition_id}`",
                f"- **Auth method:** {format_auth_method(connection.auth_method)}",
                f"- **Tenant ID:** `{connection.tenant_id}`",
                f"- **Client ID:** `{connection.client_id}`",
            ]
        )
    )

    health_error = get_health_error(st.session_state)
    if health_error:
        st.error(f"Last connection test failed: {health_error}")
        st.info("Update the settings and run Test Connection again.")
        return

    health_results = get_health_results(st.session_state)
    health_summary = summarize_health(health_results)
    if overall_state == "not_tested":
        st.info(
            "Connection settings are saved for this session, but the ADME "
            "services have not been validated yet."
        )
        return

    if overall_state == "healthy":
        st.success(
            f"All {health_summary.total_services} configured OSDU services "
            "responded successfully."
        )
    elif overall_state == "degraded":
        st.warning(
            f"{health_summary.unhealthy_services} service(s) need attention."
        )
    else:
        st.error(
            f"{health_summary.error_services} service probe(s) failed before a "
            "response was returned."
        )

    st.subheader("Latest service health")
    st.dataframe(
        results_to_table_rows(health_results),
        use_container_width=True,
        hide_index=True,
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


def main() -> None:
    """Build the grouped navigation and run the selected page."""
    st.set_page_config(
        page_title="ADME Control Plane",
        page_icon="⚡",
        layout="wide",
    )

    home_page = st.Page(
        _render_home,
        title="Home",
        icon="🏠",
        default=True,
    )
    instance_config_page = st.Page(
        INSTANCE_CONFIG_PAGE_PATH,
        title="Instance Configuration",
        icon="⚙️",
    )
    entitlements_page = st.Page(
        ENTITLEMENTS_PAGE_PATH,
        title="Entitlements",
        icon="🔑",
    )
    legal_tags_page = st.Page(
        LEGAL_TAGS_PAGE_PATH,
        title="Legal Tags",
        icon="🏷️",
    )
    ingest_landing_page = st.Page(
        INGEST_PAGE_PATH,
        title="Overview",
        icon="🧭",
    )
    manifest_page = st.Page(
        INGESTION_PAGE_PATH,
        title="Manifest",
        icon="📄",
    )
    file_page = st.Page(
        FILE_UPLOAD_PAGE_PATH,
        title="File",
        icon="📂",
    )
    volve_page = st.Page(
        VOLVE_PAGE_PATH,
        title="Volve Dataset",
        icon="🌊",
    )
    bulk_load_page = st.Page(
        BULK_LOAD_PAGE_PATH,
        title="Bulk Load",
        icon="📥",
    )
    tno_page = st.Page(
        TNO_PAGE_PATH,
        title="TNO Dataset",
        icon="📦",
    )
    search_page = st.Page(
        SEARCH_PAGE_PATH,
        title="Search",
        icon="🔍",
    )
    history_page = st.Page(
        HISTORY_PAGE_PATH,
        title="History",
        icon="📊",
    )

    nav = st.navigation(
        {
            "": [home_page],
            "Setup": [
                instance_config_page,
                entitlements_page,
                legal_tags_page,
            ],
            "Ingest": [
                ingest_landing_page,
                manifest_page,
                file_page,
                volve_page,
                tno_page,
                bulk_load_page,
            ],
            "Operate": [search_page, history_page],
        }
    )
    nav.run()


if __name__ == "__main__":
    main()
