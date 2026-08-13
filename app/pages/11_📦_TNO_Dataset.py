"""TNO dataset ingestion orchestrator page.

Complete, resumable ingestion of:
1. Master-data generation from CSVs
2. Master-data to Storage
3. Work-products via DAG (async)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if PROJECT_ROOT not in {Path(path or ".").resolve() for path in sys.path}:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # type: ignore[import-not-found]  # noqa: E402

from app.connection_state import (  # noqa: E402
    ensure_session_defaults,
    get_connection,
)
from app.models.connection import ADMEConnection, AuthMethod  # noqa: E402
from app.services.tno_ingestion import TNOIngestionConfig, TNOIngestionOrchestrator  # noqa: E402

SETTINGS_PAGE_PATH = "pages/1_⚙️_Instance_Configuration.py"
INGEST_PAGE_PATH = "pages/4_📥_Ingest.py"

logger = logging.getLogger(__name__)


def main() -> None:
    """Render the TNO ingestion orchestrator page."""
    st.set_page_config(
        page_title="TNO Ingestion · ADME Control Plane",
        page_icon="📦",
        layout="wide",
    )
    st.title("📦 TNO Dataset Ingestion")
    st.markdown(
        "Complete, resumable ingestion of the TNO dataset: master-data generation, "
        "Storage ingestion, and work-product loading via DAG."
    )

    ensure_session_defaults(st.session_state)

    connection = get_connection(st.session_state)
    if not _preflight_ok(connection):
        return
    assert connection is not None  # mypy

    st.caption(
        f"Data partition: `{connection.data_partition_id}` · "
        f"Endpoint: `{connection.endpoint}`"
    )

    # Initialize session state
    if "tno_orchestrator" not in st.session_state:
        st.session_state.tno_orchestrator = None
    if "tno_running" not in st.session_state:
        st.session_state.tno_running = False
    if "tno_result" not in st.session_state:
        st.session_state.tno_result = None
    if "tno_date_prefix" not in st.session_state:
        st.session_state.tno_date_prefix = ""

    _render_configuration()
    _render_controls(connection)
    _render_status()


def _preflight_ok(connection: ADMEConnection | None) -> bool:
    """Return True when the session has a usable ADME connection."""
    if connection is None or not connection.is_valid():
        st.info(
            "No ADME connection is configured for this session. "
            "Open Instance Configuration to add your endpoint, identity "
            "details, and data partition."
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


def _render_configuration() -> None:
    """Render configuration options."""
    with st.expander("⚙️ Configuration", expanded=False):
        col1, col2, col3 = st.columns(3)

        with col1:
            tno_root = st.text_input(
                "TNO root path",
                value=str(Path.home() / "osdu-data" / "tno"),
                help="Path containing TNO/provided and datasets",
            )

        with col2:
            skip_generate = st.checkbox(
                "Skip generation",
                value=False,
                help="If CSVs already converted",
            )

        with col3:
            skip_master_data = st.checkbox(
                "Skip master-data",
                value=False,
                help="If already ingested",
            )

        skip_work_products = st.checkbox(
            "Skip work-products",
            value=False,
            help="If already submitted",
        )

        max_concurrency = st.slider(
            "Max concurrent work-product submissions",
            min_value=1,
            max_value=16,
            value=8,
            help="Higher = faster but may throttle; default 8",
        )

        include_v110 = st.checkbox(
            "Include v1.1.0 schema variants",
            value=False,
            help="Load alternate schema-version work-products",
        )

        # Date prefix for test organization
        st.markdown("---")
        st.markdown("**Test Organization**")
        date_prefix = st.text_input(
            "Date/Test prefix",
            value=st.session_state.tno_date_prefix,
            placeholder="e.g., 2026-08-13_regression, 2026-08-13_smoke",
            help="Optional prefix to organize test runs (logged but not used in ingestion)",
        )
        st.session_state.tno_date_prefix = date_prefix

        st.session_state.tno_config = {
            "tno_root": Path(tno_root),
            "skip_generate": skip_generate,
            "skip_master_data": skip_master_data,
            "skip_work_products": skip_work_products,
            "max_concurrency": max_concurrency,
            "include_v110": include_v110,
            "date_prefix": date_prefix,
        }


def _render_controls(connection: ADMEConnection) -> None:
    """Render run controls."""
    st.subheader("Ingestion Control")

    config_dict = st.session_state.get("tno_config", {})
    date_prefix = config_dict.get("date_prefix", "")
    prefix_display = f"[{date_prefix}] " if date_prefix else ""

    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button(f"▶️ Start Ingestion {prefix_display}", key="start_btn", use_container_width=True):
            _start_ingestion(connection)
            st.rerun()

    with col2:
        if st.button(f"🔄 Resume (Checkpoint) {prefix_display}", key="resume_btn", use_container_width=True):
            _resume_ingestion(connection)
            st.rerun()

    with col3:
        if st.button("🧹 Clear Checkpoint", key="clear_btn", use_container_width=True):
            checkpoint_path = Path(__file__).resolve().parent.parent.parent / "scripts" / ".tno_ingestion_checkpoint.json"
            if checkpoint_path.exists():
                checkpoint_path.unlink()
                st.success("Checkpoint cleared")
            else:
                st.info("No checkpoint found")


def _start_ingestion(connection: ADMEConnection) -> None:
    """Start a new ingestion run."""
    config_dict = st.session_state.get("tno_config", {})
    
    config = TNOIngestionConfig(
        endpoint=connection.endpoint,
        data_partition_id=connection.data_partition_id,
        skip_generate=config_dict.get("skip_generate", False),
        skip_master_data=config_dict.get("skip_master_data", False),
        skip_work_products=config_dict.get("skip_work_products", False),
        max_concurrency=config_dict.get("max_concurrency", 8),
        include_v110=config_dict.get("include_v110", False),
        tno_root=config_dict.get("tno_root", Path.home() / "osdu-data" / "tno"),
    )

    # Validate configuration
    if not config.tno_root.is_dir():
        st.error(f"TNO root not found: {config.tno_root}")
        return

    with st.spinner("Running TNO ingestion..."):
        try:
            orchestrator = TNOIngestionOrchestrator(config)
            
            # Log the date prefix if provided
            date_prefix = config_dict.get("date_prefix", "")
            if date_prefix:
                logger.info(f"Test run prefix: {date_prefix}")
            
            success = orchestrator.run()

            if success:
                st.success("✓ TNO ingestion completed successfully!")
                st.session_state.tno_result = orchestrator.state
            else:
                st.error("✗ TNO ingestion failed. Check logs for details.")
                st.session_state.tno_result = orchestrator.state

        except Exception as e:
            st.error(f"Ingestion error: {str(e)}")
            logger.exception("TNO ingestion error")


def _resume_ingestion(connection: ADMEConnection) -> None:
    """Resume a previous ingestion run from checkpoint."""
    config_dict = st.session_state.get("tno_config", {})
    
    config = TNOIngestionConfig(
        endpoint=connection.endpoint,
        data_partition_id=connection.data_partition_id,
        skip_generate=config_dict.get("skip_generate", False),
        skip_master_data=config_dict.get("skip_master_data", False),
        skip_work_products=config_dict.get("skip_work_products", False),
        max_concurrency=config_dict.get("max_concurrency", 8),
        include_v110=config_dict.get("include_v110", False),
        tno_root=config_dict.get("tno_root", Path.home() / "osdu-data" / "tno"),
    )

    with st.spinner("Resuming TNO ingestion from checkpoint..."):
        try:
            orchestrator = TNOIngestionOrchestrator(config)
            
            # Log the date prefix if provided
            date_prefix = config_dict.get("date_prefix", "")
            if date_prefix:
                logger.info(f"Test run prefix (resumed): {date_prefix}")
            
            if orchestrator.state.started_at:
                st.info(f"Resuming from checkpoint (started {orchestrator.state.started_at})")
            
            success = orchestrator.run()

            if success:
                st.success("✓ TNO ingestion resumed and completed successfully!")
                st.session_state.tno_result = orchestrator.state
            else:
                st.error("✗ TNO ingestion resume failed. Check logs for details.")
                st.session_state.tno_result = orchestrator.state

        except Exception as e:
            st.error(f"Ingestion error: {str(e)}")
            logger.exception("TNO ingestion error")


def _render_status() -> None:
    """Render ingestion status and results."""
    if st.session_state.tno_result is None:
        st.info("No ingestion result yet. Click Start or Resume to begin.")
        return

    state = st.session_state.tno_result
    
    st.subheader("📊 Ingestion Results")

    # Summary metrics
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            "Generation",
            f"{state.generate_ok}/3",
            "✓" if state.generate_completed else "⏳",
        )
        st.caption("⏱️ ~2 min")

    with col2:
        total_md = state.master_data_ok + state.master_data_failed
        st.metric(
            "Master-Data",
            f"{state.master_data_ok}/{total_md}" if total_md > 0 else "—",
            "✓" if (state.master_data_completed and state.master_data_failed == 0) else "⚠" if state.master_data_failed > 0 else "⏳",
        )
        st.caption("⏱️ ~5 min")

    with col3:
        st.metric(
            "Work-Products",
            f"{state.work_products_submitted}",
            "✓" if state.work_products_completed else "⏳",
        )
        st.caption("⏱️ ~13 min")

    with col4:
        errors_badge = "⚠" if state.errors else "✓"
        st.metric(
            "Errors",
            len(state.errors),
            errors_badge,
        )
        st.caption("ℹ️ Check log")

    # Detailed breakdown
    with st.expander("📋 Detailed Breakdown"):
        tab1, tab2, tab3, tab4 = st.tabs(["Generation", "Master-Data", "Work-Products", "Errors"])

        with tab1:
            st.write(f"**Status**: {'✓ Completed' if state.generate_completed else '⏳ In Progress'}")
            st.write(f"**Manifests**: {state.generate_ok}/3 (Organisation, Well, Wellbore)")

        with tab2:
            st.write(f"**Status**: {'✓ Completed' if state.master_data_completed else '⏳ In Progress'}")
            st.write(f"**Success**: {state.master_data_ok}")
            st.write(f"**Failed**: {state.master_data_failed}")

        with tab3:
            st.write(f"**Status**: {'✓ Completed' if state.work_products_completed else '⏳ In Progress'}")
            st.write(f"**Submitted**: {state.work_products_submitted}")
            st.write(f"**Success**: {state.work_products_ok}")
            st.write(f"**Failed**: {state.work_products_failed}")

        with tab4:
            if state.errors:
                st.error(f"{len(state.errors)} error(s) encountered:")
                for err in state.errors[:10]:
                    st.code(err)
            else:
                st.success("No errors")

    # Log file link
    log_file = Path(__file__).resolve().parent.parent.parent / "scripts" / "tno_ingest_complete.log"
    if log_file.exists():
        with st.expander("📝 View Full Log"):
            with open(log_file) as f:
                st.code(f.read()[-2000:])  # Last 2000 chars


if __name__ == "__main__":
    main()
