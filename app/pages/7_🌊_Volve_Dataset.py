"""Volve dataset ingestion orchestrator page.

Complete, resumable ingestion of:
1. Seismic data to SDMS (48 datasets)
2. Core metadata to Storage (164 records)
3. Wellbore/Well records to DDMS (38 records)
"""

from __future__ import annotations

import logging
import subprocess
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
from app.services.volve_ingestion import VolveIngestionConfig, VolveIngestionOrchestrator  # noqa: E402

SETTINGS_PAGE_PATH = "pages/1_⚙️_Instance_Configuration.py"
INGEST_PAGE_PATH = "pages/4_📥_Ingest.py"

logger = logging.getLogger(__name__)


def main() -> None:
    """Render the Volve ingestion orchestrator page."""
    st.set_page_config(
        page_title="Volve Ingestion · ADME Control Plane",
        page_icon="🌊",
        layout="wide",
    )
    st.title("🌊 Volve Dataset Ingestion")
    st.markdown(
        "Complete, resumable ingestion of the Volve dataset across three platforms: "
        "SDMS (seismic), Storage (metadata), and Wellbore DDMS."
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
    if "volve_orchestrator" not in st.session_state:
        st.session_state.volve_orchestrator = None
    if "volve_running" not in st.session_state:
        st.session_state.volve_running = False
    if "volve_result" not in st.session_state:
        st.session_state.volve_result = None
    if "volve_date_prefix" not in st.session_state:
        st.session_state.volve_date_prefix = ""

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

    if connection.auth_method == AuthMethod.USER_IMPERSONATION:
        # This requires user auth which should be available in the session
        pass

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
            generated_data_root = st.text_input(
                "Generated data root",
                value=str(Path.home() / "osdu-data" / "volve" / "generated-json"),
                help="Path to prepared Volve manifests",
            )

        with col2:
            skip_seismic = st.checkbox(
                "Skip seismic ingestion",
                value=False,
                help="If already ingested",
            )

        with col3:
            skip_wellbore = st.checkbox(
                "Skip Wellbore DDMS",
                value=False,
                help="If already ingested",
            )

        metadata_only = st.checkbox(
            "Metadata only (skip DAG workflows)",
            value=False,
            help="Speed up core metadata ingestion",
        )

        # Date prefix for test organization
        st.markdown("---")
        st.markdown("**Test Organization**")
        date_prefix = st.text_input(
            "Date/Test prefix",
            value=st.session_state.volve_date_prefix,
            placeholder="e.g., 2026-08-13_regression, 2026-08-13_smoke",
            help="Optional prefix to organize test runs (logged but not used in ingestion)",
        )
        st.session_state.volve_date_prefix = date_prefix

        st.session_state.volve_config = {
            "generated_data_root": Path(generated_data_root),
            "skip_seismic": skip_seismic,
            "skip_wellbore": skip_wellbore,
            "metadata_only": metadata_only,
            "date_prefix": date_prefix,
        }


def _render_controls(connection: ADMEConnection) -> None:
    """Render run controls."""
    st.subheader("Ingestion Control")

    config_dict = st.session_state.get("volve_config", {})
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
            checkpoint_path = Path(__file__).resolve().parent.parent.parent / "scripts" / ".volve_ingestion_checkpoint.json"
            if checkpoint_path.exists():
                checkpoint_path.unlink()
                st.success("Checkpoint cleared")
            else:
                st.info("No checkpoint found")


def _start_ingestion(connection: ADMEConnection) -> None:
    """Start a new ingestion run."""
    config_dict = st.session_state.get("volve_config", {})
    
    config = VolveIngestionConfig(
        endpoint=connection.endpoint,
        data_partition_id=connection.data_partition_id,
        skip_seismic=config_dict.get("skip_seismic", False),
        skip_wellbore=config_dict.get("skip_wellbore", False),
        metadata_only=config_dict.get("metadata_only", False),
        generated_data_root=config_dict.get("generated_data_root", Path.home() / "osdu-data" / "volve" / "generated-json"),
    )

    # Validate configuration
    if not config.generated_data_root.is_dir():
        st.error(f"Generated data root not found: {config.generated_data_root}")
        st.info("Run `python scripts/prepare_volve_data.py` first")
        return

    with st.spinner("Running Volve ingestion..."):
        try:
            orchestrator = VolveIngestionOrchestrator(config)
            
            # Log the date prefix if provided
            date_prefix = config_dict.get("date_prefix", "")
            if date_prefix:
                logger.info(f"Test run prefix: {date_prefix}")
            
            success = orchestrator.run()

            if success:
                st.success("✓ Volve ingestion completed successfully!")
                st.session_state.volve_result = orchestrator.state
            else:
                st.error("✗ Volve ingestion failed. Check logs for details.")
                st.session_state.volve_result = orchestrator.state

        except Exception as e:
            st.error(f"Ingestion error: {str(e)}")
            logger.exception("Volve ingestion error")


def _resume_ingestion(connection: ADMEConnection) -> None:
    """Resume a previous ingestion run from checkpoint."""
    config_dict = st.session_state.get("volve_config", {})
    
    config = VolveIngestionConfig(
        endpoint=connection.endpoint,
        data_partition_id=connection.data_partition_id,
        skip_seismic=config_dict.get("skip_seismic", False),
        skip_wellbore=config_dict.get("skip_wellbore", False),
        metadata_only=config_dict.get("metadata_only", False),
        generated_data_root=config_dict.get("generated_data_root", Path.home() / "osdu-data" / "volve" / "generated-json"),
    )

    with st.spinner("Resuming Volve ingestion from checkpoint..."):
        try:
            orchestrator = VolveIngestionOrchestrator(config)
            
            # Log the date prefix if provided
            date_prefix = config_dict.get("date_prefix", "")
            if date_prefix:
                logger.info(f"Test run prefix (resumed): {date_prefix}")
            
            if orchestrator.state.started_at:
                st.info(f"Resuming from checkpoint (started {orchestrator.state.started_at})")
            
            success = orchestrator.run()

            if success:
                st.success("✓ Volve ingestion resumed and completed successfully!")
                st.session_state.volve_result = orchestrator.state
            else:
                st.error("✗ Volve ingestion resume failed. Check logs for details.")
                st.session_state.volve_result = orchestrator.state

        except Exception as e:
            st.error(f"Ingestion error: {str(e)}")
            logger.exception("Volve ingestion error")


def _render_status() -> None:
    """Render ingestion status and results."""
    if st.session_state.volve_result is None:
        st.info("No ingestion result yet. Click Start or Resume to begin.")
        return

    state = st.session_state.volve_result
    
    st.subheader("📊 Ingestion Results")

    # Summary metrics
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            "Seismic",
            f"{state.seismic_count}/48",
            "✓" if state.seismic_completed else "⏳",
        )
        st.caption("⏱️ ~10 min")

    with col2:
        st.metric(
            "Core Metadata",
            f"{state.core_metadata_ok}/{state.core_metadata_ok + state.core_metadata_failed}",
            "✓" if (state.core_metadata_completed and state.core_metadata_failed == 0) else "⚠" if state.core_metadata_failed > 0 else "⏳",
        )
        st.caption("⏱️ ~8 min")

    with col3:
        total_wellbore = state.wellbore_ingestion_count + state.well_ingestion_count
        st.metric(
            "Wellbore DDMS",
            f"{total_wellbore}/38",
            "✓" if state.wellbore_ddms_completed else "⏳",
        )
        st.caption("⏱️ ~5 min")

    with col4:
        st.metric(
            "Validation",
            "✓" if state.wellbore_validation_passed else "⚠",
            "" if state.wellbore_validation_passed else "Check logs",
        )
        st.caption("⏱️ ~2 min")

    # Detailed breakdown
    with st.expander("📋 Detailed Breakdown"):
        tab1, tab2, tab3, tab4 = st.tabs(["Seismic", "Core Metadata", "Wellbore DDMS", "Errors"])

        with tab1:
            st.write(f"**Status**: {'✓ Completed' if state.seismic_completed else '⏳ In Progress'}")
            st.write(f"**Datasets**: {state.seismic_count}/48")
            if state.seismic_failed:
                st.warning(f"Failed uploads: {len(state.seismic_failed)}")
                for failed in state.seismic_failed[:5]:
                    st.code(failed)

        with tab2:
            st.write(f"**Status**: {'✓ Completed' if state.core_metadata_completed else '⏳ In Progress'}")
            st.write(f"**Success**: {state.core_metadata_ok}")
            st.write(f"**Failed**: {state.core_metadata_failed}")

        with tab3:
            st.write(f"**Status**: {'✓ Completed' if state.wellbore_ddms_completed else '⏳ In Progress'}")
            st.write(f"**Wellbores**: {state.wellbore_ingestion_count}/27")
            st.write(f"**Wells**: {state.well_ingestion_count}/11")
            st.write(f"**Validation**: {'✓ Passed' if state.wellbore_validation_passed else '⚠ Warnings'}")

        with tab4:
            if state.errors:
                st.error(f"{len(state.errors)} error(s) encountered:")
                for err in state.errors:
                    st.code(err)
            else:
                st.success("No errors")

    # Log file link
    log_file = Path(__file__).resolve().parent.parent.parent / "scripts" / "volve_ingest_complete.log"
    if log_file.exists():
        with st.expander("📝 View Full Log"):
            with open(log_file) as f:
                st.code(f.read()[-2000:])  # Last 2000 chars


if __name__ == "__main__":
    main()
