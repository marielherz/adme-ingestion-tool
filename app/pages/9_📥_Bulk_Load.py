"""Bulk Load page — submit a registered OSDU dataset tier to ADME.

Wires Kevin's :mod:`app.services.bulk_loader` registry + preview + submit
generator into a Streamlit page. v1 ships reference-data only: master-data
and work-products tiers ship disabled in every dataset descriptor and are
surfaced read-only here.

The page enforces a mandatory **Preview gate**: the Submit button stays
disabled until the operator has clicked Preview for the current dataset/tier
combination. Changing dataset or tier invalidates the gate so the operator
can never submit a payload they didn't first inspect.

The **Generate from CSV** tab lets operators pick an OSDU kind, upload a
CSV, review/adjust the auto-mapped column-to-field mapping, and generate +
submit manifests without hand-authoring JSON templates.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if PROJECT_ROOT not in {Path(path or ".").resolve() for path in sys.path}:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # type: ignore[import-untyped]  # noqa: E402
import streamlit as st  # type: ignore[import-not-found]  # noqa: E402

from app.connection_state import (  # noqa: E402
    ensure_session_defaults,
    get_connection,
    get_user_auth_state,
)
from app.models.connection import (  # noqa: E402
    ADMEConnection,
    AuthMethod,
)
from app.models.osdu import (  # noqa: E402
    CircuitBreakerTripped,
    DatasetDescriptor,
    FieldMapping,
    ManifestPreview,
    MappingResult,
    QueueItem,
    QueueSubmitResult,
    QueueValidationResult,
    SubmitResult,
    WorkflowStatus,
)
from app.services.auth import AuthenticationError, get_token  # noqa: E402
from app.services.bulk_ingestion import (  # noqa: E402
    MAX_QUEUE_SIZE,
    build_queue_from_files,
    enforce_queue_size_limit,
    parse_pasted_manifests,
    submit_queue,
    validate_queue,
)
from app.services.bulk_loader import (  # noqa: E402
    DATA_ROOT,
    _clear_cache,
    list_datasets,
    make_load_prefix,
    preview_tier,
    submit_manifest_paths,
    submit_tier,
)
from app.services.downloaded_dataset import (  # noqa: E402
    DownloadedPart,
    discover_parts,
    list_part_manifests,
)
from app.services.entitlements import fetch_groups  # noqa: E402
from app.services.ingestion import get_workflow_status, submit_manifest  # noqa: E402
from app.services.legal_tags import list_legal_tags  # noqa: E402
from app.services.manifest_generator import (  # noqa: E402
    MappingError,
    SchemaNotFoundError,
    auto_map,
    extract_schema_fields,
    generate_manifests,
    list_schema_kinds,
    load_schema,
)
from app.services.work_product_loader import (  # noqa: E402
    submit_work_products,
)

SETTINGS_PAGE_PATH = "pages/1_⚙️_Instance_Configuration.py"

# --- Locked session-state keys (tests assert these names) ----------------
BULK_DATASET_KEY = "bulk_dataset_id"
BULK_TIER_KEY = "bulk_tier"
BULK_LEGAL_TAG_KEY = "bulk_legal_tag"
BULK_ACL_OWNERS_KEY = "bulk_acl_owners"
BULK_ACL_VIEWERS_KEY = "bulk_acl_viewers"
BULK_LOAD_PREFIX_KEY = "bulk_load_prefix"  # str — per-load independent-copy id prefix
BULK_PREVIEW_SEEN_KEY = "bulk_preview_seen"  # tuple[str, str] | None
BULK_PREVIEW_RESULTS_KEY = "bulk_preview_results"  # list[ManifestPreview]
BULK_SUBMIT_RESULTS_KEY = "bulk_submit_results"  # list[SubmitResult]
BULK_RUN_STATUS_KEY = "bulk_run_status"  # dict[run_id, {state, detail}]
BULK_LAST_ERROR_KEY = "bulk_last_error"  # str | None
BULK_ABORT_KEY = "bulk_abort_requested"  # bool — graceful mid-loop stop

# --- Downloaded-dataset (external TNO/Volve root) session keys ------------
DOWNLOAD_ROOT_KEY = "bulk_download_root"  # str — local download folder
DOWNLOAD_PART_KEY = "bulk_download_part"  # int — selected part index
DOWNLOAD_LIMIT_KEY = "bulk_download_limit"  # int — manifest cap (0 = all)


# --- Generate-from-CSV session-state keys (prefixed gen_) ----------------
GEN_KIND_KEY = "gen_kind"
GEN_CSV_DATA_KEY = "gen_csv_data"
GEN_MAPPING_RESULT_KEY = "gen_mapping_result"
GEN_CONFIRMED_MAPPINGS_KEY = "gen_confirmed_mappings"
GEN_MANIFESTS_KEY = "gen_manifests"
GEN_SUBMIT_RESULTS_KEY = "gen_submit_results"
GEN_LEGAL_TAG_KEY = "gen_legal_tag"
GEN_ACL_OWNERS_KEY = "gen_acl_owners"
GEN_ACL_VIEWERS_KEY = "gen_acl_viewers"
GEN_LAST_ERROR_KEY = "gen_last_error"
GEN_ABORT_KEY = "gen_abort_requested"  # bool — graceful mid-loop stop (CSV tab)

# --- Internal helper keys for CSV-gen options ----------------------------
GEN_OPTIONS_AUTORUN_KEY = "gen_options_autorun_done"
GEN_LEGAL_TAG_OPTIONS_KEY = "gen_legal_tag_options"
GEN_ACL_OWNER_OPTIONS_KEY = "gen_acl_owner_options"
GEN_ACL_VIEWER_OPTIONS_KEY = "gen_acl_viewer_options"

# --- Internal helper keys (not part of the locked contract) --------------
BULK_OPTIONS_AUTORUN_KEY = "bulk_options_autorun_done"
BULK_LEGAL_TAG_OPTIONS_KEY = "bulk_legal_tag_options"
BULK_ACL_OWNER_OPTIONS_KEY = "bulk_acl_owner_options"
BULK_ACL_VIEWER_OPTIONS_KEY = "bulk_acl_viewer_options"

PREVIEW_BUTTON_LABEL = "🔍 Preview manifests"
SUBMIT_BUTTON_LABEL = "🚀 Submit all manifests"
RUN_STATUS_BUTTON_LABEL = "🔄 Check ingestion status"
DISMISS_BUTTON_LABEL = "Dismiss error"
REFRESH_OPTIONS_LABEL = "🔄 Refresh legal tags & groups"

# --- Queue tab session-state keys (locked — tests assert these names) ----
QUEUE_INPUT_MODE_KEY = "queue_input_mode"
QUEUE_UPLOADED_FILES_KEY = "queue_uploaded_files"
QUEUE_PASTE_TEXT_KEY = "queue_paste_text"
QUEUE_LEGAL_TAG_KEY = "queue_legal_tag"
QUEUE_ACL_OWNERS_KEY = "queue_acl_owners"
QUEUE_ACL_VIEWERS_KEY = "queue_acl_viewers"
QUEUE_INTER_SUBMIT_DELAY_KEY = "queue_inter_submit_delay"
QUEUE_SKIP_INVALID_KEY = "queue_skip_invalid"
QUEUE_PARSED_ITEMS_KEY = "queue_parsed_items"
QUEUE_VALIDATION_RESULTS_KEY = "queue_validation_results"
QUEUE_PREVIEW_SEEN_KEY = "queue_preview_seen"  # bool — reviewed checkbox
QUEUE_LIVE_RESULTS_KEY = "queue_live_results"
QUEUE_LIVE_ATTEMPTS_KEY = "queue_live_attempts"
QUEUE_BREAKER_EVENT_KEY = "queue_breaker_event"
QUEUE_LAST_BATCH_SUMMARY_KEY = "queue_last_batch_summary"
QUEUE_ABORT_KEY = "queue_abort_requested"
QUEUE_SUBMIT_IN_FLIGHT_KEY = "queue_submit_in_flight"

# Queue helper keys (autorun-once dropdown options + signature/last-mode).
QUEUE_LEGAL_TAG_OPTIONS_KEY = "queue_legal_tag_options"
QUEUE_ACL_OWNER_OPTIONS_KEY = "queue_acl_owner_options"
QUEUE_ACL_VIEWER_OPTIONS_KEY = "queue_acl_viewer_options"
QUEUE_OPTIONS_AUTORUN_KEY = "queue_options_autorun_done"
QUEUE_LAST_MODE_KEY = "queue_last_input_mode"
QUEUE_INPUT_SIGNATURE_KEY = "queue_input_signature"

QUEUE_INPUT_MODE_UPLOAD = "Multi-file upload"
QUEUE_INPUT_MODE_PASTE = "Paste many (--- separator)"
QUEUE_FILE_UPLOADER_LABEL = "Manifest files"
QUEUE_PASTE_TEXTAREA_LABEL = "Paste manifests (separate each with a line of `---`)"
QUEUE_PARSE_BUTTON_LABEL = "🔎 Parse queue"
QUEUE_SUBMIT_BUTTON_LABEL = "🚀 Submit queue"
QUEUE_ABORT_BUTTON_LABEL = "⏹️ Abort queue"
QUEUE_PREVIEW_CHECKBOX_LABEL = "I have reviewed the queue"
QUEUE_RESUME_BUTTON_LABEL = "▶️ Resume after breaker"
QUEUE_DOWNLOAD_FAILED_LABEL = "⬇️ Download failed rows (JSON)"
QUEUE_REFRESH_OPTIONS_LABEL = "🔄 Refresh legal tags & groups (queue)"

# Row state → operator emoji mapping for the live progress board.
_QUEUE_ROW_STATE_EMOJI: dict[str, str] = {
    "queued": "⏸",
    "submitting": "⏳",
    "retrying": "🟡",
    "success": "✅",
    "error": "❌",
    "rejected": "❌",
    "skipped": "⏹",
    "skipped_invalid": "⛔",
    "skipped_breaker": "🛑",
    "breaker_tripped": "🛑",
}


def main() -> None:
    """Render the Bulk Load page."""
    st.set_page_config(
        page_title="Bulk Load · ADME Control Plane",
        page_icon="📥",
        layout="wide",
    )
    st.title("📥 Bulk Load")
    st.markdown(
        "Submit a registered OSDU dataset (reference-data, master-data, or "
        "work-products) to your ADME instance, or generate manifests from a "
        "CSV file. **v1 supports reference-data only.**"
    )

    ensure_session_defaults(st.session_state)
    _ensure_page_defaults()

    # Drop the registry cache on mount so freshly dropped dataset folders
    # appear without an app restart (per Satya §1).
    _clear_cache()

    connection = get_connection(st.session_state)
    if not _preflight_ok(connection):
        return
    assert connection is not None  # mypy — _preflight_ok guarantees this

    st.caption(
        f"Data partition: `{connection.data_partition_id}` · "
        f"Endpoint: `{connection.endpoint}`"
    )

    tab_datasets, tab_download, tab_csv, tab_queue = st.tabs(
        [
            "📦 Registered Datasets",
            "📂 Downloaded Dataset",
            "📄 Generate from CSV",
            "📋 Queue",
        ]
    )

    with tab_datasets:
        _render_registered_datasets_tab(connection)

    with tab_download:
        _render_downloaded_dataset_tab(connection)

    with tab_csv:
        _render_csv_generation_tab(connection)

    with tab_queue:
        _render_queue_tab(connection)


def _render_registered_datasets_tab(connection: ADMEConnection) -> None:
    """Render the original Registered Datasets workflow."""
    _render_sticky_error()

    datasets = list_datasets()
    if not datasets:
        st.warning(
            "No datasets are registered on disk. Add a folder under "
            "`app/data/datasets/<id>/` with a `dataset.json` descriptor."
        )
        return

    descriptor = _render_dataset_selector(datasets)
    _render_source_and_license(descriptor)
    tier_name = _render_tier_selector(descriptor)

    _render_input_form(connection)

    if tier_name is None:
        st.info(
            "No tiers are enabled for this dataset yet. Pick a different "
            "dataset or wait for the next vendor drop."
        )
        return

    # If the dataset or tier changed since the last preview, invalidate
    # the gate so the operator must Preview again before Submit.
    seen = st.session_state.get(BULK_PREVIEW_SEEN_KEY)
    current_key = (descriptor.id, tier_name)
    if seen is not None and seen != current_key:
        st.session_state[BULK_PREVIEW_SEEN_KEY] = None
        st.session_state[BULK_PREVIEW_RESULTS_KEY] = []

    _render_preview_section(descriptor, tier_name)
    _render_submit_section(connection, descriptor, tier_name)
    _render_results_section(connection)


# ---------------------------------------------------------------------------
# Downloaded dataset (external TNO/Volve root)
# ---------------------------------------------------------------------------


def _render_downloaded_dataset_tab(connection: ADMEConnection) -> None:
    """Load a downloaded OSDU dataset (TNO/Volve) from a local folder.

    Unlike the bundled registry this reads manifests straight from the
    operator's download root, uploads work-product blobs, and overwrites the
    placeholder ACL/legal the vendor manifests ship with.
    """
    _render_sticky_error()
    st.markdown(
        "Load a **downloaded** dataset (e.g. TNO or Volve from the Azure "
        "`osdu-data-load-tno` tool) directly from a local folder. Point at "
        "the download root — the folder that contains `TNO/` and `datasets/`."
    )

    root_str = str(
        st.text_input(
            "Download root folder",
            key=DOWNLOAD_ROOT_KEY,
            placeholder=r"C:\Users\you\osdu-data\tno",
            help=(
                "Local path to the downloaded dataset. Manifests are read "
                "from `TNO/provided/**`; work-product blobs from `datasets/**`."
            ),
        )
        or ""
    ).strip()
    if not root_str:
        st.info("Enter the folder where you downloaded the dataset.")
        return

    root = Path(root_str)
    if not root.is_dir():
        st.error(f"Folder not found: `{root}`")
        return

    parts = discover_parts(root)
    if not parts:
        st.warning(
            "No loadable manifests found. Expected "
            "`TNO/provided/{reference-data,master-data,work-products}/`."
        )
        return

    part_index = st.selectbox(
        "Part to load",
        options=range(len(parts)),
        format_func=lambda i: parts[i].label,
        key=DOWNLOAD_PART_KEY,
    )
    part = parts[int(part_index or 0)]

    limit = int(
        st.number_input(
            "Limit (0 = all)",
            min_value=0,
            step=1,
            key=DOWNLOAD_LIMIT_KEY,
            help=(
                "Cap the number of manifests — use a small value (e.g. 9 for "
                "documents) for a smoke batch before the full load."
            ),
        )
        or 0
    )
    manifests = list_part_manifests(part, limit=limit)

    kind_note = (
        "uploads each file blob, then submits"
        if part.is_work_product
        else "submits list-section manifests"
    )
    st.caption(
        f"**{len(manifests)}** of {part.manifest_count} manifest(s) — "
        f"{kind_note}. ACL/legal below are **overwritten** onto every record "
        "(the vendor manifests ship placeholder groups)."
    )

    _render_input_form(connection)

    reason = _download_disabled_reason(manifests)
    is_disabled = reason is not None
    clicked = st.button(
        "🚀 Load selected part",
        key="bulk_download_load_button",
        type="primary",
        disabled=is_disabled,
        help="Uploads blobs (work-products) and submits each manifest.",
    )
    if is_disabled and reason is not None:
        st.caption(f"⏸️ {reason}")

    if clicked:
        _run_download_load(connection, part, manifests)

    _render_results_section(connection)


def _download_disabled_reason(manifests: Sequence[Path]) -> str | None:
    """Return why the download Load button is disabled, or ``None``."""
    if not manifests:
        return "No manifests match the current selection."
    if not str(st.session_state.get(BULK_LEGAL_TAG_KEY) or "").strip():
        return "Select a legal tag."
    if not str(st.session_state.get(BULK_ACL_OWNERS_KEY) or "").strip():
        return "Fill ACL owners group."
    if not str(st.session_state.get(BULK_ACL_VIEWERS_KEY) or "").strip():
        return "Fill ACL viewers group."
    return None


def _run_download_load(
    connection: ADMEConnection,
    part: DownloadedPart,
    manifests: Sequence[Path],
) -> None:
    """Stream a downloaded-dataset load (list tier or work-products)."""
    _clear_sticky_error()
    token = _acquire_token(connection)
    if token is None:
        return

    legal_tag = str(st.session_state.get(BULK_LEGAL_TAG_KEY) or "").strip()
    acl_owners = [str(st.session_state.get(BULK_ACL_OWNERS_KEY) or "").strip()]
    acl_viewers = [
        str(st.session_state.get(BULK_ACL_VIEWERS_KEY) or "").strip()
    ]
    load_prefix = str(st.session_state.get(BULK_LOAD_PREFIX_KEY) or "").strip()

    total = len(manifests)
    results: list[SubmitResult] = []

    st.write(f"Loading {total} manifest(s) from **{part.label}**…")
    if load_prefix and part.is_work_product:
        st.warning(
            "⚠️ Work-products are already independent per load (server-minted "
            "ids), but their master-data references aren't prefixed yet — "
            "leave the prefix blank unless the matching master-data was also "
            "loaded under it."
        )
    st.session_state[BULK_ABORT_KEY] = False
    st.button(
        "⏹️ Abort",
        key="bulk_download_abort_btn",
        on_click=_set_bulk_abort,
        help="Stop after the current manifest finishes.",
    )

    aborted = False
    try:
        if part.is_work_product:
            iterator = submit_work_products(
                manifests,
                datasets_root=part.datasets_root,
                acl_owners=acl_owners,
                acl_viewers=acl_viewers,
                legal_tag=legal_tag,
                data_partition_id=connection.data_partition_id,
                connection=connection,
                token=token,
            )
        else:
            iterator = submit_manifest_paths(
                manifests,
                section=part.section or "ReferenceData",
                acl_owners=acl_owners,
                acl_viewers=acl_viewers,
                legal_tag=legal_tag,
                data_partition_id=connection.data_partition_id,
                connection=connection,
                token=token,
                load_prefix=load_prefix,
                overwrite_acl_legal=True,
            )

        for index, result in enumerate(iterator, start=1):
            results.append(result)
            st.session_state[BULK_SUBMIT_RESULTS_KEY] = list(results)
            st.write(f"**{index} of {total}** — `{result.filename}`")
            _render_submit_row(result)
            if st.session_state.get(BULK_ABORT_KEY):
                aborted = True
                break
    except ValueError as exc:
        _set_sticky_error(f"Load aborted: {exc}")
        st.session_state[BULK_SUBMIT_RESULTS_KEY] = results
        st.rerun()
        return
    except Exception as exc:  # noqa: BLE001 - operator-safe summary
        _set_sticky_error(
            f"Unexpected error during load: {type(exc).__name__}: {exc}"
        )
        st.session_state[BULK_SUBMIT_RESULTS_KEY] = results
        st.rerun()
        return

    st.session_state[BULK_SUBMIT_RESULTS_KEY] = results
    if aborted:
        st.warning(f"⏹️ Aborted after {len(results)} of {total} manifests.")


# ---------------------------------------------------------------------------
# Session bootstrap
# ---------------------------------------------------------------------------


def _ensure_page_defaults() -> None:
    """Initialize page-scoped session keys."""
    st.session_state.setdefault(BULK_DATASET_KEY, "")
    st.session_state.setdefault(BULK_TIER_KEY, "")
    st.session_state.setdefault(BULK_LEGAL_TAG_KEY, "")
    st.session_state.setdefault(BULK_ACL_OWNERS_KEY, "")
    st.session_state.setdefault(BULK_ACL_VIEWERS_KEY, "")
    st.session_state.setdefault(BULK_LOAD_PREFIX_KEY, "")
    st.session_state.setdefault(DOWNLOAD_ROOT_KEY, "")
    st.session_state.setdefault(DOWNLOAD_LIMIT_KEY, 0)
    st.session_state.setdefault(BULK_PREVIEW_SEEN_KEY, None)
    st.session_state.setdefault(BULK_PREVIEW_RESULTS_KEY, [])
    st.session_state.setdefault(BULK_SUBMIT_RESULTS_KEY, [])
    st.session_state.setdefault(BULK_RUN_STATUS_KEY, {})
    st.session_state.setdefault(BULK_LAST_ERROR_KEY, None)
    st.session_state.setdefault(BULK_ABORT_KEY, False)

    st.session_state.setdefault(BULK_OPTIONS_AUTORUN_KEY, False)
    st.session_state.setdefault(BULK_LEGAL_TAG_OPTIONS_KEY, None)
    st.session_state.setdefault(BULK_ACL_OWNER_OPTIONS_KEY, None)
    st.session_state.setdefault(BULK_ACL_VIEWER_OPTIONS_KEY, None)

    # Generate-from-CSV defaults
    st.session_state.setdefault(GEN_KIND_KEY, "")
    st.session_state.setdefault(GEN_CSV_DATA_KEY, None)
    st.session_state.setdefault(GEN_MAPPING_RESULT_KEY, None)
    st.session_state.setdefault(GEN_CONFIRMED_MAPPINGS_KEY, None)
    st.session_state.setdefault(GEN_MANIFESTS_KEY, None)
    st.session_state.setdefault(GEN_SUBMIT_RESULTS_KEY, [])
    st.session_state.setdefault(GEN_LEGAL_TAG_KEY, "")
    st.session_state.setdefault(GEN_ACL_OWNERS_KEY, "")
    st.session_state.setdefault(GEN_ACL_VIEWERS_KEY, "")
    st.session_state.setdefault(GEN_LAST_ERROR_KEY, None)
    st.session_state.setdefault(GEN_ABORT_KEY, False)
    st.session_state.setdefault(GEN_OPTIONS_AUTORUN_KEY, False)
    st.session_state.setdefault(GEN_LEGAL_TAG_OPTIONS_KEY, None)
    st.session_state.setdefault(GEN_ACL_OWNER_OPTIONS_KEY, None)
    st.session_state.setdefault(GEN_ACL_VIEWER_OPTIONS_KEY, None)

    # Queue tab defaults
    st.session_state.setdefault(QUEUE_PARSED_ITEMS_KEY, None)
    st.session_state.setdefault(QUEUE_VALIDATION_RESULTS_KEY, None)
    st.session_state.setdefault(QUEUE_PREVIEW_SEEN_KEY, False)
    st.session_state.setdefault(QUEUE_LIVE_RESULTS_KEY, [])
    st.session_state.setdefault(QUEUE_LIVE_ATTEMPTS_KEY, {})
    st.session_state.setdefault(QUEUE_BREAKER_EVENT_KEY, None)
    st.session_state.setdefault(QUEUE_LAST_BATCH_SUMMARY_KEY, None)
    st.session_state.setdefault(QUEUE_ABORT_KEY, False)
    st.session_state.setdefault(QUEUE_SUBMIT_IN_FLIGHT_KEY, False)
    st.session_state.setdefault(QUEUE_OPTIONS_AUTORUN_KEY, False)
    st.session_state.setdefault(QUEUE_LEGAL_TAG_OPTIONS_KEY, None)
    st.session_state.setdefault(QUEUE_ACL_OWNER_OPTIONS_KEY, None)
    st.session_state.setdefault(QUEUE_ACL_VIEWER_OPTIONS_KEY, None)
    st.session_state.setdefault(QUEUE_LAST_MODE_KEY, "")
    st.session_state.setdefault(QUEUE_INPUT_SIGNATURE_KEY, "")


# ---------------------------------------------------------------------------
# Pre-flight (mirrors Manifest page exactly)
# ---------------------------------------------------------------------------


def _preflight_ok(connection: ADMEConnection | None) -> bool:
    """Return True when we have everything required to run Bulk Load."""
    if connection is None or not connection.is_valid():
        st.info(
            "No ADME connection is configured for this session. "
            "Open Instance Configuration to add your endpoint, identity details, "
            "and data partition."
        )
        st.page_link(
            SETTINGS_PAGE_PATH,
            label="Open Instance Configuration",
            icon="⚙️",
        )
        return False

    if connection.auth_method == AuthMethod.USER_IMPERSONATION:
        if get_user_auth_state(st.session_state) is None:
            st.info(
                "No token available for this session. Sign in on the "
                "Instance Configuration page to enable Bulk Load."
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
# Sticky errors (same idiom as Manifest / Legal Tags pages)
# ---------------------------------------------------------------------------


def _set_sticky_error(message: str) -> None:
    st.session_state[BULK_LAST_ERROR_KEY] = message


def _clear_sticky_error() -> None:
    st.session_state[BULK_LAST_ERROR_KEY] = None


def _render_sticky_error() -> None:
    message = st.session_state.get(BULK_LAST_ERROR_KEY)
    if not message:
        return
    st.error(message)
    if st.button(DISMISS_BUTTON_LABEL, key="bulk_dismiss_error"):
        _clear_sticky_error()
        st.rerun()


# ---------------------------------------------------------------------------
# Dataset selector + source/license expander
# ---------------------------------------------------------------------------


def _render_dataset_selector(
    datasets: list[DatasetDescriptor],
) -> DatasetDescriptor:
    """Render the dataset dropdown and return the selected descriptor."""
    options = [d.id for d in datasets]
    labels = {d.id: d.display_name for d in datasets}

    # Default to the first dataset if nothing is selected yet, or if the
    # previously selected id is gone from the registry.
    current = str(st.session_state.get(BULK_DATASET_KEY) or "")
    if current not in options:
        st.session_state[BULK_DATASET_KEY] = options[0]

    selected_id = st.selectbox(
        "Dataset",
        options=options,
        format_func=lambda i: labels.get(i, i),
        key=BULK_DATASET_KEY,
        help="Datasets discovered under `app/data/datasets/<id>/dataset.json`.",
    )
    return next(d for d in datasets if d.id == selected_id)


def _render_source_and_license(descriptor: DatasetDescriptor) -> None:
    """Render the source URL + NOTICE.md expander for this dataset."""
    with st.expander("📄 Source & license", expanded=False):
        st.markdown(f"**Source:** [{descriptor.source_url}]({descriptor.source_url})")
        notice_text = _read_notice(descriptor)
        if notice_text is None:
            st.caption("NOTICE not available")
        else:
            st.markdown(notice_text)


def _read_notice(descriptor: DatasetDescriptor) -> str | None:
    """Return the NOTICE.md body for this dataset, or ``None`` if missing.

    The notice path is resolved under ``DATA_ROOT`` defensively — even for
    in-tree datasets we never read a file outside ``app/data/``.
    """
    try:
        candidate = (descriptor.root_dir / descriptor.notice_path).resolve()
        candidate.relative_to(DATA_ROOT)
        return candidate.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Tier selector
# ---------------------------------------------------------------------------


def _render_tier_selector(descriptor: DatasetDescriptor) -> str | None:
    """Render the tier radio. Returns the selected tier name or ``None``.

    Only enabled tiers appear in the radio. Disabled tiers are listed in an
    ``st.info`` block underneath so the operator sees what's coming next
    without being able to submit against them.
    """
    enabled_tiers = [
        name for name, tier in descriptor.tiers.items() if tier.enabled
    ]
    disabled_tiers = [
        (name, tier.reason or "tier disabled")
        for name, tier in descriptor.tiers.items()
        if not tier.enabled
    ]

    selected: str | None = None
    if enabled_tiers:
        current = str(st.session_state.get(BULK_TIER_KEY) or "")
        if current not in enabled_tiers:
            st.session_state[BULK_TIER_KEY] = enabled_tiers[0]
        selected = st.radio(
            "Tier",
            options=enabled_tiers,
            key=BULK_TIER_KEY,
            horizontal=True,
            help="v1 supports reference-data only.",
        )

    if disabled_tiers:
        bullets = "\n".join(
            f"- **{name}** — {reason}" for name, reason in disabled_tiers
        )
        st.info("Disabled tiers (future):\n\n" + bullets)

    return selected


# ---------------------------------------------------------------------------
# Legal tag + ACL inputs (selectbox-with-fallback, mirrors Manifest page)
# ---------------------------------------------------------------------------


def _render_input_form(connection: ADMEConnection) -> None:
    """Render the legal-tag / ACL inputs."""
    refresh_clicked = st.button(
        REFRESH_OPTIONS_LABEL,
        key="bulk_refresh_options",
        help="Re-fetch legal tags and entitlement groups from ADME.",
    )
    if refresh_clicked:
        _load_input_options(connection, force=True)
        st.rerun()
    else:
        _load_input_options(connection)

    legal_options = st.session_state.get(BULK_LEGAL_TAG_OPTIONS_KEY)
    owner_options = st.session_state.get(BULK_ACL_OWNER_OPTIONS_KEY)
    viewer_options = st.session_state.get(BULK_ACL_VIEWER_OPTIONS_KEY)

    cols = st.columns(3)
    with cols[0]:
        _render_option_field(
            label="Legal tag name",
            session_key=BULK_LEGAL_TAG_KEY,
            options=legal_options,
            placeholder="opendes-tno-data",
            help_text=(
                "Fully qualified legal tag. Applied to every record that "
                "doesn't already carry one."
            ),
            empty_caption="⚠️ Couldn't load legal tags — enter manually",
        )
    with cols[1]:
        _render_option_field(
            label="ACL owners group",
            session_key=BULK_ACL_OWNERS_KEY,
            options=owner_options,
            placeholder="data.default.owners@opendes.dataservices.energy",
            help_text="Entitlements group that should own these records.",
            empty_caption="⚠️ Couldn't load groups — enter manually",
        )
    with cols[2]:
        _render_option_field(
            label="ACL viewers group",
            session_key=BULK_ACL_VIEWERS_KEY,
            options=viewer_options,
            placeholder="data.default.viewers@opendes.dataservices.energy",
            help_text="Entitlements group allowed to read these records.",
            empty_caption="⚠️ Couldn't load groups — enter manually",
        )

    _render_load_prefix_field()


def _render_load_prefix_field() -> None:
    """Render the optional per-load id prefix input (Smart Tier copies).

    Leave blank for a normal idempotent reload (same ids → upsert). Set a
    distinct prefix — e.g. today's date — to load the dataset as an
    *independent* copy whose records age on their own tier clock, which is
    what the Smart Tier test plan needs for its Day 0 / 30 / 90 loads.
    """
    suggested = make_load_prefix()
    st.text_input(
        "Load prefix (optional)",
        key=BULK_LOAD_PREFIX_KEY,
        placeholder=f"e.g. {suggested}",
        help=(
            "Prepended to every record's unique id (and the references "
            "between them) so this submission is an independent copy. "
            "Leave blank to reload over the existing records. Use a "
            "distinct value per Smart Tier load — today's date is "
            f"`{suggested}`."
        ),
    )


def _load_input_options(
    connection: ADMEConnection, *, force: bool = False
) -> None:
    """Autorun-once load of legal tags + entitlement groups for dropdowns."""
    if not force and st.session_state.get(BULK_OPTIONS_AUTORUN_KEY, False):
        return

    token = _acquire_token(connection)
    if token is None:
        st.session_state[BULK_OPTIONS_AUTORUN_KEY] = True
        return

    try:
        legal_result = list_legal_tags(connection, token, valid=True)
        if legal_result.ok and legal_result.items:
            names = sorted({t.name for t in legal_result.items if t.name})
            st.session_state[BULK_LEGAL_TAG_OPTIONS_KEY] = names or None
        else:
            st.session_state[BULK_LEGAL_TAG_OPTIONS_KEY] = None
    except Exception:  # noqa: BLE001
        st.session_state[BULK_LEGAL_TAG_OPTIONS_KEY] = None

    try:
        groups_result = fetch_groups(connection, token)
        owners, viewers = _partition_acl_groups(groups_result)
        st.session_state[BULK_ACL_OWNER_OPTIONS_KEY] = owners or None
        st.session_state[BULK_ACL_VIEWER_OPTIONS_KEY] = viewers or None
    except Exception:  # noqa: BLE001
        st.session_state[BULK_ACL_OWNER_OPTIONS_KEY] = None
        st.session_state[BULK_ACL_VIEWER_OPTIONS_KEY] = None

    st.session_state[BULK_OPTIONS_AUTORUN_KEY] = True


def _partition_acl_groups(groups_result: Any) -> tuple[list[str], list[str]]:
    """Split a fetch_groups result into sorted owner / viewer email lists."""
    if not getattr(groups_result, "ok", False):
        return [], []
    data = getattr(groups_result, "data", None)
    if not isinstance(data, dict):
        return [], []
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list):
        return [], []

    owners: set[str] = set()
    viewers: set[str] = set()
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        email = group.get("email")
        if not isinstance(email, str) or "@" not in email:
            continue
        local = email.split("@", 1)[0]
        if not local.startswith("data."):
            continue
        if local.endswith(".owners"):
            owners.add(email)
        elif local.endswith(".viewers"):
            viewers.add(email)
    return sorted(owners), sorted(viewers)


def _render_option_field(
    *,
    label: str,
    session_key: str,
    options: list[str] | None,
    placeholder: str,
    help_text: str,
    empty_caption: str,
) -> None:
    """Render a selectbox when options loaded; otherwise a text_input fallback."""
    if not options:
        st.text_input(
            label,
            key=session_key,
            placeholder=placeholder,
            help=help_text,
        )
        st.caption(empty_caption)
        return

    current = str(st.session_state.get(session_key) or "")
    final_options: list[str] = [""] + list(options)
    if current and current not in final_options:
        final_options.append(current)
    st.selectbox(
        label,
        options=final_options,
        key=session_key,
        help=help_text,
    )


# ---------------------------------------------------------------------------
# Preview gate
# ---------------------------------------------------------------------------


def _render_preview_section(
    descriptor: DatasetDescriptor, tier_name: str
) -> None:
    """Render the Preview button + results table."""
    clicked = st.button(
        PREVIEW_BUTTON_LABEL,
        key="bulk_preview_button",
        help="Read manifests from disk, count records — no network call.",
    )

    if clicked:
        _clear_sticky_error()
        try:
            fresh_previews = preview_tier(descriptor.id, tier_name)
        except ValueError as exc:
            _set_sticky_error(f"Cannot preview {tier_name!r}: {exc}")
            st.session_state[BULK_PREVIEW_RESULTS_KEY] = []
            st.session_state[BULK_PREVIEW_SEEN_KEY] = None
            st.rerun()
            return
        st.session_state[BULK_PREVIEW_RESULTS_KEY] = fresh_previews
        st.session_state[BULK_PREVIEW_SEEN_KEY] = (descriptor.id, tier_name)
        # Reset prior submit results so the page state is coherent.
        st.session_state[BULK_SUBMIT_RESULTS_KEY] = []

    previews: list[ManifestPreview] = st.session_state.get(
        BULK_PREVIEW_RESULTS_KEY, []
    )
    seen = st.session_state.get(BULK_PREVIEW_SEEN_KEY)
    if seen != (descriptor.id, tier_name):
        return

    if not previews:
        st.caption("No manifests matched this tier's glob.")
        return

    total_records = sum(p.record_count for p in previews)
    st.success(
        f"**{len(previews)} manifests, {total_records:,} total records** "
        f"will be submitted."
    )
    frame = pd.DataFrame(
        [
            {
                "filename": p.filename,
                "kind": p.kind,
                "record_count": p.record_count,
            }
            for p in previews
        ]
    )
    st.dataframe(frame, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def _submit_disabled_reason(
    descriptor: DatasetDescriptor, tier_name: str
) -> str | None:
    """Return a human-readable reason Submit is disabled, or ``None`` when enabled."""
    seen = st.session_state.get(BULK_PREVIEW_SEEN_KEY)
    if seen != (descriptor.id, tier_name):
        return "Run Preview first to inspect manifests before submitting."
    if not str(st.session_state.get(BULK_LEGAL_TAG_KEY) or "").strip():
        return "Select a legal tag."
    if not str(st.session_state.get(BULK_ACL_OWNERS_KEY) or "").strip():
        return "Fill ACL owners group."
    if not str(st.session_state.get(BULK_ACL_VIEWERS_KEY) or "").strip():
        return "Fill ACL viewers group."
    return None


def _render_submit_section(
    connection: ADMEConnection,
    descriptor: DatasetDescriptor,
    tier_name: str,
) -> None:
    """Render the Submit button (gated) and run the loop on click."""
    disabled_reason = _submit_disabled_reason(descriptor, tier_name)
    is_disabled = disabled_reason is not None

    clicked = st.button(
        SUBMIT_BUTTON_LABEL,
        key="bulk_submit_button",
        type="primary",
        disabled=is_disabled,
        help=(
            "Sequentially submits every previewed manifest. "
            "Each result is recorded to Run History."
        ),
    )

    if is_disabled and disabled_reason is not None:
        st.caption(f"⏸️ {disabled_reason}")

    if not clicked:
        return

    _run_submit(connection, descriptor, tier_name)


def _set_bulk_abort() -> None:
    """``on_click`` callback — sets the graceful abort flag."""
    st.session_state[BULK_ABORT_KEY] = True


def _set_gen_abort() -> None:
    """``on_click`` callback — sets the graceful abort flag (CSV tab)."""
    st.session_state[GEN_ABORT_KEY] = True


def _run_submit(
    connection: ADMEConnection,
    descriptor: DatasetDescriptor,
    tier_name: str,
) -> None:
    """Acquire a token, iterate ``submit_tier``, render progress, store results."""
    _clear_sticky_error()
    token = _acquire_token(connection)
    if token is None:
        return

    legal_tag = str(st.session_state.get(BULK_LEGAL_TAG_KEY) or "").strip()
    acl_owners = [
        str(st.session_state.get(BULK_ACL_OWNERS_KEY) or "").strip()
    ]
    acl_viewers = [
        str(st.session_state.get(BULK_ACL_VIEWERS_KEY) or "").strip()
    ]
    load_prefix = str(st.session_state.get(BULK_LOAD_PREFIX_KEY) or "").strip()

    previews: list[ManifestPreview] = st.session_state.get(
        BULK_PREVIEW_RESULTS_KEY, []
    )
    total = len(previews)
    results: list[SubmitResult] = []

    st.write(f"Submitting {total} manifest(s)…")
    if load_prefix:
        st.caption(
            f"🔁 Independent copy — record ids prefixed with `{load_prefix}`."
        )
    st.button(
        "⏹️ Abort",
        key="bulk_abort_btn",
        on_click=_set_bulk_abort,
        help="Stop after the current manifest finishes.",
    )

    aborted = False
    try:
        iterator = submit_tier(
            descriptor.id,
            tier_name,
            acl_owners=acl_owners,
            acl_viewers=acl_viewers,
            legal_tag=legal_tag,
            data_partition_id=connection.data_partition_id,
            connection=connection,
            token=token,
            load_prefix=load_prefix,
        )
        for index, result in enumerate(iterator, start=1):
            results.append(result)
            st.session_state[BULK_SUBMIT_RESULTS_KEY] = list(results)
            st.write(
                f"**{index} of {total}** — `{result.filename}`"
            )
            _render_submit_row(result)

            # Graceful abort: finish current HTTP call, skip remaining.
            if st.session_state.get(BULK_ABORT_KEY):
                aborted = True
                break
    except ValueError as exc:
        _set_sticky_error(f"Submit aborted: {exc}")
        st.session_state[BULK_SUBMIT_RESULTS_KEY] = results
        st.rerun()
        return
    except Exception as exc:  # noqa: BLE001 - operator-safe summary
        _set_sticky_error(
            f"Unexpected error during submit: {type(exc).__name__}: {exc}"
        )
        st.session_state[BULK_SUBMIT_RESULTS_KEY] = results
        st.rerun()
        return

    st.session_state[BULK_SUBMIT_RESULTS_KEY] = results
    if aborted:
        st.warning(
            f"⏹️ Aborted after {len(results)} of {total} manifests."
        )


def _render_submit_row(result: SubmitResult) -> None:
    """Render one ✅/❌ result row inline as it streams in."""
    if result.status == "success":
        run_label = result.run_id or "(no run id)"
        st.markdown(f"✅ `{result.filename}` → runId: `{run_label}`")
    else:
        st.markdown(
            f"❌ `{result.filename}` → {result.error or 'unknown error'}"
        )


def _render_results_section(connection: ADMEConnection) -> None:
    """Render the persistent summary of the last submit batch."""
    results: list[SubmitResult] = st.session_state.get(
        BULK_SUBMIT_RESULTS_KEY, []
    )
    if not results:
        return

    succeeded = sum(1 for r in results if r.status == "success")
    failed = len(results) - succeeded

    st.subheader("Submit results")

    # Show abort indicator when results are partial.
    if st.session_state.get(BULK_ABORT_KEY):
        previews_for_abort: list[ManifestPreview] = st.session_state.get(
            BULK_PREVIEW_RESULTS_KEY, []
        )
        if previews_for_abort and len(results) < len(previews_for_abort):
            st.warning(
                f"⏹️ Aborted after {len(results)} of "
                f"{len(previews_for_abort)} manifests."
            )

    summary = f"{succeeded} of {len(results)} succeeded"
    if failed == 0:
        st.success(summary)
    else:
        st.warning(f"{summary} — {failed} failed.")

    frame = pd.DataFrame(
        [
            {
                "filename": r.filename,
                "status": r.status,
                "run_id": r.run_id or "",
                "record_id": r.record_id or "",
                "error": r.error or "",
                "submitted_at": r.submitted_at.isoformat()
                if r.submitted_at
                else "",
            }
            for r in results
        ]
    )
    st.dataframe(frame, use_container_width=True, hide_index=True)

    _render_ingestion_status_section(connection)


# ---------------------------------------------------------------------------
# Ingestion (workflow run) status — closes the submit→ingest loop
# ---------------------------------------------------------------------------

_RUN_STATE_ICON = {
    "finished": "✅",
    "running": "🟡",
    "failed": "❌",
    "unknown": "⚪",
}


def _workflow_state_label(status: WorkflowStatus) -> str:
    """Map a normalized WorkflowStatus to a compact UI state string."""
    if status == WorkflowStatus.FINISHED:
        return "finished"
    if status == WorkflowStatus.FAILED:
        return "failed"
    if status == WorkflowStatus.IN_PROGRESS:
        return "running"
    return "unknown"


def _render_ingestion_status_section(connection: ADMEConnection) -> None:
    """Render the per-run ingestion status with a manual refresh.

    A successful *submit* only means each manifest was accepted and a
    workflow run started. This polls the Workflow Service for each run id so
    operators can see ingestion actually reach ``finished`` (or ``failed``).
    """
    results: list[SubmitResult] = st.session_state.get(
        BULK_SUBMIT_RESULTS_KEY, []
    )
    run_ids = [r.run_id for r in results if r.status == "success" and r.run_id]
    if not run_ids:
        return

    st.markdown("### Ingestion status")
    st.caption(
        "Submitted manifests run asynchronously. A successful submit means "
        "*accepted*, not *finished* — check that each workflow run reaches "
        "✅ finished."
    )

    if st.button(
        RUN_STATUS_BUTTON_LABEL,
        key="bulk_run_status_button",
        help="Polls the Workflow Service status for each submitted run id.",
    ):
        _check_ingestion_status(connection, run_ids)

    statuses: dict[str, dict[str, str]] = st.session_state.get(
        BULK_RUN_STATUS_KEY, {}
    )
    if not statuses:
        st.caption("Click to poll the Workflow Service for each run.")
        return

    counts = {"finished": 0, "running": 0, "failed": 0, "unknown": 0}
    for run_id in run_ids:
        state = statuses.get(run_id, {}).get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1

    rollup = (
        f"✅ {counts['finished']} finished · 🟡 {counts['running']} running · "
        f"❌ {counts['failed']} failed · ⚪ {counts['unknown']} unknown"
    )
    if counts["failed"] or counts["unknown"]:
        st.warning(rollup)
    elif counts["running"]:
        st.info(rollup)
    else:
        st.success(rollup)

    status_frame = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "status": (
                    f"{_RUN_STATE_ICON.get(state, '⚪')} {state}"
                ),
                "detail": statuses.get(run_id, {}).get("detail", ""),
            }
            for run_id in run_ids
            for state in [statuses.get(run_id, {}).get("state", "unknown")]
        ]
    )
    st.dataframe(status_frame, use_container_width=True, hide_index=True)


def _check_ingestion_status(
    connection: ADMEConnection, run_ids: list[str]
) -> None:
    """Poll the Workflow Service for each run id and store the states."""
    token = _acquire_token(connection)
    if token is None:
        return

    statuses: dict[str, dict[str, str]] = {}
    total = len(run_ids)
    progress = st.progress(0.0, text="Checking run status…")
    for index, run_id in enumerate(run_ids, start=1):
        try:
            result = get_workflow_status(connection, token, run_id)
        except Exception as exc:  # noqa: BLE001 - operator-safe summary
            statuses[run_id] = {
                "state": "unknown",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        else:
            if result.ok:
                statuses[run_id] = {
                    "state": _workflow_state_label(result.status),
                    "detail": result.raw_status or result.message or "",
                }
            else:
                statuses[run_id] = {
                    "state": "unknown",
                    "detail": (
                        result.error_message
                        or f"HTTP {result.http_status}"
                    ),
                }
        progress.progress(index / total, text=f"Checked {index} of {total}")

    st.session_state[BULK_RUN_STATUS_KEY] = statuses


# ===========================================================================
# Generate from CSV tab
# ===========================================================================


def _render_csv_generation_tab(connection: ADMEConnection) -> None:
    """Render the Generate from CSV workflow."""
    _render_gen_sticky_error()

    # --- Step 1: Kind selector ---
    kinds = list_schema_kinds()
    if not kinds:
        st.warning(
            "No vendored schemas found. Check that "
            "`app/data/osdu/rc--3.0.0/schemas/` contains schema JSON files."
        )
        return

    selected_kind = st.selectbox(
        "OSDU kind",
        options=[""] + kinds,
        key=GEN_KIND_KEY,
        help="Select the OSDU kind that matches your CSV data.",
    )

    if not selected_kind:
        st.info("Select an OSDU kind to begin.")
        return

    # --- Step 2: CSV upload ---
    uploaded_file = st.file_uploader(
        "Upload CSV",
        type=["csv"],
        key="gen_csv_uploader",
        help="Upload the CSV file containing the data to ingest.",
    )

    if uploaded_file is not None:
        csv_bytes = uploaded_file.getvalue()
        # Reset downstream state when CSV changes
        prev_csv = st.session_state.get(GEN_CSV_DATA_KEY)
        if prev_csv != csv_bytes:
            st.session_state[GEN_CSV_DATA_KEY] = csv_bytes
            st.session_state[GEN_MAPPING_RESULT_KEY] = None
            st.session_state[GEN_CONFIRMED_MAPPINGS_KEY] = None
            st.session_state[GEN_MANIFESTS_KEY] = None
            st.session_state[GEN_SUBMIT_RESULTS_KEY] = []

    csv_data: bytes | None = st.session_state.get(GEN_CSV_DATA_KEY)
    if csv_data is None:
        st.info("Upload a CSV file to continue.")
        return

    # --- Step 3: Auto-map ---
    try:
        csv_headers = _parse_csv_headers(csv_data)
    except ValueError as exc:
        st.error(f"Could not parse CSV headers: {exc}")
        return

    mapping_result: MappingResult | None = st.session_state.get(
        GEN_MAPPING_RESULT_KEY
    )
    if mapping_result is None:
        try:
            schema = load_schema(selected_kind)
            schema_fields = extract_schema_fields(schema)
            mapping_result = auto_map(schema_fields, csv_headers)
            st.session_state[GEN_MAPPING_RESULT_KEY] = mapping_result
        except SchemaNotFoundError as exc:
            st.error(f"Schema not available: {exc}")
            return

    # --- Step 4: Editable mapping table ---
    st.subheader("Column mapping")
    confidence_pct = int(mapping_result.confidence * 100)
    if confidence_pct >= 80:
        st.success(f"Auto-map confidence: **{confidence_pct}%**")
    elif confidence_pct >= 50:
        st.warning(f"Auto-map confidence: **{confidence_pct}%** — review suggested")
    else:
        st.error(
            f"Auto-map confidence: **{confidence_pct}%** — "
            "manual adjustment recommended"
        )

    schema = load_schema(selected_kind)
    schema_fields = extract_schema_fields(schema)
    field_options = ["(unmapped)"] + csv_headers

    confirmed: list[FieldMapping] = []
    for sf in schema_fields:
        # Find current mapping for this field
        current_csv_col = "(unmapped)"
        for m in mapping_result.mappings:
            if m.schema_path == sf.path:
                current_csv_col = m.csv_header
                break

        default_index = 0
        if current_csv_col in field_options:
            default_index = field_options.index(current_csv_col)

        req_marker = " ⚠️" if sf.required else ""
        chosen = st.selectbox(
            f"{sf.path} ({sf.field_type}){req_marker}",
            options=field_options,
            index=default_index,
            key=f"gen_map_{sf.path}",
            help=sf.description or f"Schema field: {sf.path}",
        )
        if chosen != "(unmapped)":
            confirmed.append(
                FieldMapping(csv_header=chosen, schema_path=sf.path)
            )

    st.session_state[GEN_CONFIRMED_MAPPINGS_KEY] = confirmed

    if mapping_result.unmatched_required:
        # Check which required fields are still unmapped after operator edits
        mapped_paths = {m.schema_path for m in confirmed}
        still_unmapped = [
            r for r in mapping_result.unmatched_required
            if r not in mapped_paths
        ]
        if still_unmapped:
            st.warning(
                f"**{len(still_unmapped)} required field(s) unmapped:** "
                + ", ".join(f"`{f}`" for f in still_unmapped)
            )

    if mapping_result.unmatched_csv:
        with st.expander("Unmatched CSV columns", expanded=False):
            for col in mapping_result.unmatched_csv:
                st.caption(f"• {col}")

    # --- Step 5: Legal tag + ACL ---
    st.subheader("Legal & ACL")
    _render_gen_input_form(connection)

    # --- Step 6: Generate manifests ---
    gen_disabled_reason = _gen_generate_disabled_reason(confirmed)
    gen_is_disabled = gen_disabled_reason is not None

    gen_clicked = st.button(
        "📄 Generate Manifests",
        key="gen_generate_button",
        disabled=gen_is_disabled,
        help="Generate OSDU manifests from the CSV using the confirmed mapping.",
    )
    if gen_is_disabled and gen_disabled_reason:
        st.caption(f"⏸️ {gen_disabled_reason}")

    if gen_clicked:
        _run_generate(selected_kind, csv_data, confirmed, connection)

    # --- Step 7: Summary + Submit ---
    manifests: list[dict] | None = st.session_state.get(GEN_MANIFESTS_KEY)
    if manifests:
        st.subheader("Generated manifests")
        st.success(
            f"**{len(manifests)} manifest(s)** generated and ready to submit."
        )
        with st.expander(
            f"📋 Sample manifest (1 of {len(manifests)})", expanded=False
        ):
            st.json(manifests[0])

        submit_disabled_reason = _gen_submit_disabled_reason()
        submit_is_disabled = submit_disabled_reason is not None

        submit_clicked = st.button(
            "🚀 Submit generated manifests",
            key="gen_submit_button",
            type="primary",
            disabled=submit_is_disabled,
            help="Submit all generated manifests to the ADME ingestion pipeline.",
        )
        if submit_is_disabled and submit_disabled_reason:
            st.caption(f"⏸️ {submit_disabled_reason}")

        if submit_clicked:
            _run_gen_submit(connection, manifests)

    # --- Step 8: Submission results ---
    _render_gen_results_section()


# ---------------------------------------------------------------------------
# CSV generation helpers
# ---------------------------------------------------------------------------


def _parse_csv_headers(csv_bytes: bytes) -> list[str]:
    """Extract header row from CSV bytes. Raises ValueError if empty."""
    text = csv_bytes.decode("utf-8-sig")
    reader = io.StringIO(text)
    try:
        headers = next(iter(__import__("csv").reader(reader)))
    except StopIteration:
        raise ValueError("CSV file is empty — no header row found.")
    if not headers or all(h.strip() == "" for h in headers):
        raise ValueError("Upload a CSV with headers.")
    return [h.strip() for h in headers]


def _gen_generate_disabled_reason(
    confirmed: list[FieldMapping],
) -> str | None:
    """Return a reason the Generate button is disabled, or None."""
    if not confirmed:
        return "Map at least one CSV column to a schema field."
    legal = str(st.session_state.get(GEN_LEGAL_TAG_KEY) or "").strip()
    if not legal:
        return "Select a legal tag."
    owners = str(st.session_state.get(GEN_ACL_OWNERS_KEY) or "").strip()
    if not owners:
        return "Fill ACL owners group."
    viewers = str(st.session_state.get(GEN_ACL_VIEWERS_KEY) or "").strip()
    if not viewers:
        return "Fill ACL viewers group."
    return None


def _gen_submit_disabled_reason() -> str | None:
    """Return a reason the Submit button is disabled, or None."""
    legal = str(st.session_state.get(GEN_LEGAL_TAG_KEY) or "").strip()
    if not legal:
        return "Select a legal tag."
    owners = str(st.session_state.get(GEN_ACL_OWNERS_KEY) or "").strip()
    if not owners:
        return "Fill ACL owners group."
    viewers = str(st.session_state.get(GEN_ACL_VIEWERS_KEY) or "").strip()
    if not viewers:
        return "Fill ACL viewers group."
    manifests = st.session_state.get(GEN_MANIFESTS_KEY)
    if not manifests:
        return "Generate manifests first."
    return None


def _run_generate(
    kind: str,
    csv_data: bytes,
    mapping: list[FieldMapping],
    connection: ADMEConnection,
) -> None:
    """Run generate_manifests and store results in session state."""
    _clear_gen_sticky_error()
    legal_tag = str(st.session_state.get(GEN_LEGAL_TAG_KEY) or "").strip()
    acl_owners = str(st.session_state.get(GEN_ACL_OWNERS_KEY) or "").strip()
    acl_viewers = str(st.session_state.get(GEN_ACL_VIEWERS_KEY) or "").strip()

    try:
        # Write CSV bytes to a temp file for generate_manifests
        with tempfile.NamedTemporaryFile(
            suffix=".csv", delete=False, mode="wb"
        ) as tmp:
            tmp.write(csv_data)
            tmp_path = Path(tmp.name)

        manifests = generate_manifests(
            kind=kind,
            csv_path=tmp_path,
            mapping=mapping,
            legal_tag=legal_tag,
            acl_owners=acl_owners,
            acl_viewers=acl_viewers,
            data_partition_id=connection.data_partition_id,
        )
        st.session_state[GEN_MANIFESTS_KEY] = manifests
        st.session_state[GEN_SUBMIT_RESULTS_KEY] = []
    except SchemaNotFoundError as exc:
        _set_gen_sticky_error(f"Schema not available: {exc}")
        st.rerun()
    except MappingError as exc:
        _set_gen_sticky_error(f"Mapping error: {exc}")
        st.rerun()
    except ValueError as exc:
        _set_gen_sticky_error(f"Generation error: {exc}")
        st.rerun()
    except Exception as exc:  # noqa: BLE001 - operator-safe summary
        _set_gen_sticky_error(
            f"Unexpected error: {type(exc).__name__}: {exc}"
        )
        st.rerun()
    finally:
        # Clean up temp file
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _run_gen_submit(
    connection: ADMEConnection,
    manifests: list[dict],
) -> None:
    """Submit each generated manifest via the ingestion pipeline."""
    _clear_gen_sticky_error()
    token = _acquire_token(connection)
    if token is None:
        return

    total = len(manifests)
    results: list[dict[str, Any]] = []

    progress_bar = st.progress(0.0, text="Submitting manifests…")
    st.button(
        "⏹️ Abort",
        key="gen_abort_btn",
        on_click=_set_gen_abort,
        help="Stop after the current manifest finishes.",
    )

    aborted = False
    for index, manifest in enumerate(manifests, start=1):
        try:
            run_result = submit_manifest(connection, token, manifest)
            results.append(
                {
                    "index": index,
                    "ok": run_result.ok,
                    "run_id": run_result.run_id or "",
                    "error": run_result.error_message or "",
                }
            )
            status_icon = "✅" if run_result.ok else "❌"
            st.write(
                f"**{index}/{total}** {status_icon} "
                f"runId: `{run_result.run_id or '(none)'}`"
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "index": index,
                    "ok": False,
                    "run_id": "",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            st.write(f"**{index}/{total}** ❌ {type(exc).__name__}: {exc}")
        progress_bar.progress(index / total, text=f"Submitted {index}/{total}")
        st.session_state[GEN_SUBMIT_RESULTS_KEY] = list(results)

        # Graceful abort: finish current HTTP call, skip remaining.
        if st.session_state.get(GEN_ABORT_KEY):
            aborted = True
            break

    st.session_state[GEN_SUBMIT_RESULTS_KEY] = results
    if aborted:
        st.warning(
            f"⏹️ Aborted after {len(results)} of {total} manifests."
        )


def _render_gen_results_section() -> None:
    """Render persistent summary of the last CSV-generated submission."""
    results: list[dict[str, Any]] = st.session_state.get(
        GEN_SUBMIT_RESULTS_KEY, []
    )
    if not results:
        return

    succeeded = sum(1 for r in results if r.get("ok"))
    failed = len(results) - succeeded

    st.subheader("Submission results")

    # Show abort indicator when results are partial.
    if st.session_state.get(GEN_ABORT_KEY):
        gen_manifests: list[dict] | None = st.session_state.get(GEN_MANIFESTS_KEY)
        if gen_manifests and len(results) < len(gen_manifests):
            st.warning(
                f"⏹️ Aborted after {len(results)} of "
                f"{len(gen_manifests)} manifests."
            )

    summary = f"{succeeded} of {len(results)} succeeded"
    if failed == 0:
        st.success(summary)
    else:
        st.warning(f"{summary} — {failed} failed.")

    frame = pd.DataFrame(results)
    st.dataframe(frame, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# CSV-gen sticky errors
# ---------------------------------------------------------------------------


def _set_gen_sticky_error(message: str) -> None:
    st.session_state[GEN_LAST_ERROR_KEY] = message


def _clear_gen_sticky_error() -> None:
    st.session_state[GEN_LAST_ERROR_KEY] = None


def _render_gen_sticky_error() -> None:
    message = st.session_state.get(GEN_LAST_ERROR_KEY)
    if not message:
        return
    st.error(message)
    if st.button("Dismiss error", key="gen_dismiss_error"):
        _clear_gen_sticky_error()
        st.rerun()


# ---------------------------------------------------------------------------
# CSV-gen input form (legal tag + ACL, mirrors bulk pattern)
# ---------------------------------------------------------------------------


def _render_gen_input_form(connection: ADMEConnection) -> None:
    """Render legal-tag / ACL inputs for the CSV-gen flow."""
    refresh_clicked = st.button(
        "🔄 Refresh legal tags & groups",
        key="gen_refresh_options",
        help="Re-fetch legal tags and entitlement groups from ADME.",
    )
    if refresh_clicked:
        _load_gen_input_options(connection, force=True)
        st.rerun()
    else:
        _load_gen_input_options(connection)

    legal_options = st.session_state.get(GEN_LEGAL_TAG_OPTIONS_KEY)
    owner_options = st.session_state.get(GEN_ACL_OWNER_OPTIONS_KEY)
    viewer_options = st.session_state.get(GEN_ACL_VIEWER_OPTIONS_KEY)

    cols = st.columns(3)
    with cols[0]:
        _render_option_field(
            label="Legal tag name",
            session_key=GEN_LEGAL_TAG_KEY,
            options=legal_options,
            placeholder="opendes-tno-data",
            help_text="Fully qualified legal tag applied to generated manifests.",
            empty_caption="⚠️ Couldn't load legal tags — enter manually",
        )
    with cols[1]:
        _render_option_field(
            label="ACL owners group",
            session_key=GEN_ACL_OWNERS_KEY,
            options=owner_options,
            placeholder="data.default.owners@opendes.dataservices.energy",
            help_text="Entitlements group that should own these records.",
            empty_caption="⚠️ Couldn't load groups — enter manually",
        )
    with cols[2]:
        _render_option_field(
            label="ACL viewers group",
            session_key=GEN_ACL_VIEWERS_KEY,
            options=viewer_options,
            placeholder="data.default.viewers@opendes.dataservices.energy",
            help_text="Entitlements group allowed to read these records.",
            empty_caption="⚠️ Couldn't load groups — enter manually",
        )


def _load_gen_input_options(
    connection: ADMEConnection, *, force: bool = False
) -> None:
    """Autorun-once load of legal tags + entitlement groups for gen dropdowns."""
    if not force and st.session_state.get(GEN_OPTIONS_AUTORUN_KEY, False):
        return

    token = _acquire_token(connection)
    if token is None:
        st.session_state[GEN_OPTIONS_AUTORUN_KEY] = True
        return

    try:
        legal_result = list_legal_tags(connection, token, valid=True)
        if legal_result.ok and legal_result.items:
            names = sorted({t.name for t in legal_result.items if t.name})
            st.session_state[GEN_LEGAL_TAG_OPTIONS_KEY] = names or None
        else:
            st.session_state[GEN_LEGAL_TAG_OPTIONS_KEY] = None
    except Exception:  # noqa: BLE001
        st.session_state[GEN_LEGAL_TAG_OPTIONS_KEY] = None

    try:
        groups_result = fetch_groups(connection, token)
        owners, viewers = _partition_acl_groups(groups_result)
        st.session_state[GEN_ACL_OWNER_OPTIONS_KEY] = owners or None
        st.session_state[GEN_ACL_VIEWER_OPTIONS_KEY] = viewers or None
    except Exception:  # noqa: BLE001
        st.session_state[GEN_ACL_OWNER_OPTIONS_KEY] = None
        st.session_state[GEN_ACL_VIEWER_OPTIONS_KEY] = None

    st.session_state[GEN_OPTIONS_AUTORUN_KEY] = True


# ===========================================================================
# Queue tab — Issue #34 (Kevin's bulk_ingestion service)
# ===========================================================================


def _set_queue_abort() -> None:
    """``on_click`` callback — sets the graceful abort flag (Queue tab)."""
    st.session_state[QUEUE_ABORT_KEY] = True


def _reset_queue_abort_and_mark_in_flight() -> None:
    """``on_click`` for Queue Submit — clears abort and marks submit running."""
    st.session_state[QUEUE_ABORT_KEY] = False
    st.session_state[QUEUE_SUBMIT_IN_FLIGHT_KEY] = True


def _queue_abort_check() -> bool:
    """Closure passed to ``submit_queue`` as ``abort_check``."""
    return bool(st.session_state.get(QUEUE_ABORT_KEY, False))


def _compute_queue_input_signature(items: list[QueueItem] | None) -> str:
    """Stable hash of the queue's parsed inputs.

    Changes when the operator re-parses different content. Used to
    invalidate the preview-gate checkbox automatically on re-parse.
    """
    if not items:
        return ""
    h = hashlib.sha256()
    for item in items:
        h.update(item.label.encode("utf-8"))
        h.update(b"\x1f")
        h.update(item.raw_text.encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def _load_queue_input_options(
    connection: ADMEConnection, *, force: bool = False
) -> None:
    """Autorun-once load of legal tags + entitlement groups for the Queue tab."""
    if not force and st.session_state.get(QUEUE_OPTIONS_AUTORUN_KEY, False):
        return

    token = _acquire_token(connection)
    if token is None:
        st.session_state[QUEUE_OPTIONS_AUTORUN_KEY] = True
        return

    try:
        legal_result = list_legal_tags(connection, token, valid=True)
        if legal_result.ok and legal_result.items:
            names = sorted({t.name for t in legal_result.items if t.name})
            st.session_state[QUEUE_LEGAL_TAG_OPTIONS_KEY] = names or None
        else:
            st.session_state[QUEUE_LEGAL_TAG_OPTIONS_KEY] = None
    except Exception:  # noqa: BLE001
        st.session_state[QUEUE_LEGAL_TAG_OPTIONS_KEY] = None

    try:
        groups_result = fetch_groups(connection, token)
        owners, viewers = _partition_acl_groups(groups_result)
        st.session_state[QUEUE_ACL_OWNER_OPTIONS_KEY] = owners or None
        st.session_state[QUEUE_ACL_VIEWER_OPTIONS_KEY] = viewers or None
    except Exception:  # noqa: BLE001
        st.session_state[QUEUE_ACL_OWNER_OPTIONS_KEY] = None
        st.session_state[QUEUE_ACL_VIEWER_OPTIONS_KEY] = None

    st.session_state[QUEUE_OPTIONS_AUTORUN_KEY] = True


def _build_failed_rows_payload(
    results: list[QueueSubmitResult], partition_id: str
) -> bytes:
    """Build a JSON download payload of failed rows.

    Includes rows with status ``error``, ``rejected`` and any row whose
    ``error_message`` starts with ``"skipped: circuit breaker"``.
    Excludes operator-aborted rows.
    """
    failed: list[dict[str, Any]] = []
    for row in results:
        err_msg = (row.error_message or "").strip()
        is_failure = row.status in ("error", "rejected")
        is_breaker_skip = (
            row.status == "skipped"
            and err_msg.startswith("skipped: circuit breaker")
        )
        if not (is_failure or is_breaker_skip):
            continue
        failed.append(
            {
                "label": row.label,
                "status": row.status,
                "run_id": row.run_id,
                "correlation_id": row.correlation_id,
                "http_status": row.http_status,
                "latency_ms": row.latency_ms,
                "error_message": row.error_message,
                "attempts": row.attempts,
                "raw_text": row.raw_text,
                "data_partition_id": partition_id,
            }
        )
    return json.dumps(failed, indent=2, default=str).encode("utf-8")


def _make_queue_progress_callback() -> Any:
    """Build a progress callback that updates session state.

    The callback never calls ``st.rerun`` — Streamlit will rerun
    automatically at the end of the script run.
    """

    def _cb(
        row_index: int,
        state: str,
        *,
        result: QueueSubmitResult | None = None,
        attempt: int | None = None,
        trip: CircuitBreakerTripped | None = None,
    ) -> None:
        attempts = dict(st.session_state.get(QUEUE_LIVE_ATTEMPTS_KEY) or {})
        if attempt is not None:
            attempts[row_index] = attempt
        attempts[f"{row_index}:state"] = state
        st.session_state[QUEUE_LIVE_ATTEMPTS_KEY] = attempts
        if trip is not None:
            st.session_state[QUEUE_BREAKER_EVENT_KEY] = trip
        # The yielded result is appended by the main loop, not here.
        del result  # unused — kept for callback signature compatibility

    return _cb


def _render_queue_progress_board(
    results: list[QueueSubmitResult],
    attempts: dict[Any, Any],
    total: int,
) -> None:
    """Render the live per-row progress board."""
    bar = st.progress(0.0)
    completed = len(results)
    bar.progress(min(1.0, completed / total) if total else 0.0)
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(results, start=1):
        emoji = _QUEUE_ROW_STATE_EMOJI.get(row.status, "•")
        rows.append(
            {
                "#": index,
                "state": f"{emoji} {row.status}",
                "label": row.label,
                "attempts": row.attempts,
                "http": row.http_status,
                "error": (row.error_message or "")[:140],
            }
        )
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    if attempts:
        last_state = attempts.get(f"{completed}:state")
        if last_state and last_state in _QUEUE_ROW_STATE_EMOJI:
            st.caption(
                f"Last event: {_QUEUE_ROW_STATE_EMOJI[last_state]} {last_state}"
            )


def _handle_parse_queue(
    mode: str,
    uploaded_files: list[Any] | None,
    pasted_text: str,
) -> None:
    """Parse + validate the operator's input. Stores into session state.

    Resets the preview-gate checkbox and the input signature so the
    operator must explicitly re-confirm the new queue before submitting.
    """
    items: list[QueueItem] = []
    parse_error: str | None = None
    try:
        if mode == QUEUE_INPUT_MODE_UPLOAD:
            files = list(uploaded_files or [])
            if not files:
                parse_error = (
                    "Upload one or more manifest files (.json) to build a queue."
                )
            else:
                items = build_queue_from_files(files)
        else:
            text = (pasted_text or "").strip()
            if not text:
                parse_error = "Paste one or more manifests separated by `---`."
            else:
                items = parse_pasted_manifests(text)
    except ValueError as exc:
        parse_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        parse_error = f"{type(exc).__name__}: {exc}"

    if parse_error is not None:
        st.error(parse_error)
        return

    try:
        enforce_queue_size_limit(items)
    except ValueError as exc:
        # Surface but still keep parsed items so the operator can see them.
        st.error(str(exc))

    validations = validate_queue(items)

    st.session_state[QUEUE_PARSED_ITEMS_KEY] = items
    st.session_state[QUEUE_VALIDATION_RESULTS_KEY] = validations
    # Reset preview gate + signature so the new queue must be confirmed.
    st.session_state[QUEUE_PREVIEW_SEEN_KEY] = False
    st.session_state[QUEUE_INPUT_SIGNATURE_KEY] = (
        _compute_queue_input_signature(items)
    )


def _iterate_submit_queue(
    *,
    items: list[QueueItem],
    validations: list[QueueValidationResult],
    acl_owners: list[str],
    acl_viewers: list[str],
    legal_tag: str,
    data_partition_id: str,
    connection: ADMEConnection,
    token: str,
    skip_invalid: bool,
    inter_submit_delay_seconds: float,
    progress_callback: Any,
    abort_check: Any,
) -> tuple[list[QueueSubmitResult], CircuitBreakerTripped | None]:
    """Drive ``submit_queue`` and capture results + any breaker trip.

    Returns the collected per-row results and the breaker event (if any).
    Catches ``CircuitBreakerTripped`` defensively in case the service
    ever escalates a trip via exception rather than callback.
    """
    collected: list[QueueSubmitResult] = []
    breaker: CircuitBreakerTripped | None = None
    try:
        iterator = submit_queue(
            items,
            validations,
            acl_owners=acl_owners,
            acl_viewers=acl_viewers,
            legal_tag=legal_tag,
            data_partition_id=data_partition_id,
            connection=connection,
            token=token,
            skip_invalid=skip_invalid,
            inter_submit_delay_seconds=inter_submit_delay_seconds,
            abort_check=abort_check,
            progress_callback=progress_callback,
        )
        for row in iterator:
            collected.append(row)
    except CircuitBreakerTripped as trip:
        breaker = trip
    return collected, breaker


def _handle_submit_queue(
    connection: ADMEConnection,
    *,
    items: list[QueueItem],
    validations: list[QueueValidationResult],
    acl_owners: list[str],
    acl_viewers: list[str],
    legal_tag: str,
    skip_invalid: bool,
    inter_submit_delay: float,
    start_index: int = 0,
) -> None:
    """Submit (or resume) the queue and record results to session state."""
    token = _acquire_token(connection)
    if token is None:
        st.session_state[QUEUE_SUBMIT_IN_FLIGHT_KEY] = False
        return

    work_items = items[start_index:]
    work_validations = validations[start_index:]

    progress_cb = _make_queue_progress_callback()
    st.session_state[QUEUE_LIVE_ATTEMPTS_KEY] = {}
    st.session_state[QUEUE_BREAKER_EVENT_KEY] = None

    results, breaker = _iterate_submit_queue(
        items=work_items,
        validations=work_validations,
        acl_owners=acl_owners,
        acl_viewers=acl_viewers,
        legal_tag=legal_tag,
        data_partition_id=connection.data_partition_id,
        connection=connection,
        token=token,
        skip_invalid=skip_invalid,
        inter_submit_delay_seconds=inter_submit_delay,
        progress_callback=progress_cb,
        abort_check=_queue_abort_check,
    )

    if breaker is not None:
        st.session_state[QUEUE_BREAKER_EVENT_KEY] = breaker

    # Merge with prior partial results (for resume).
    prior = list(st.session_state.get(QUEUE_LIVE_RESULTS_KEY) or [])
    merged = prior + results
    st.session_state[QUEUE_LIVE_RESULTS_KEY] = merged

    # Compute summary.
    succeeded = sum(1 for r in merged if r.status == "success")
    failed = sum(1 for r in merged if r.status in ("error", "rejected"))
    skipped = sum(1 for r in merged if r.status == "skipped")
    aborted = bool(st.session_state.get(QUEUE_ABORT_KEY, False))
    st.session_state[QUEUE_LAST_BATCH_SUMMARY_KEY] = {
        "total": len(merged),
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "aborted": aborted,
        "completed_count": len(merged),
        "planned_total": len(items),
    }
    st.session_state[QUEUE_SUBMIT_IN_FLIGHT_KEY] = False


def _render_queue_tab(connection: ADMEConnection) -> None:
    """Render the multi-manifest Queue tab."""
    st.header("📋 Manifest Queue")
    st.caption(
        "Submit many manifests in a single batch. Uploads or pasted blocks "
        "are parsed, validated, then sent through Kevin's bulk-ingestion "
        "service with retry + circuit-breaker protection."
    )

    # Auto-clear parsed state when the input mode changes between reruns.
    last_mode = str(st.session_state.get(QUEUE_LAST_MODE_KEY) or "")

    mode = st.radio(
        "Input mode",
        options=[QUEUE_INPUT_MODE_UPLOAD, QUEUE_INPUT_MODE_PASTE],
        key=QUEUE_INPUT_MODE_KEY,
        horizontal=True,
        help="Choose how to provide manifests to the queue.",
    )

    if last_mode and last_mode != mode:
        st.session_state[QUEUE_PARSED_ITEMS_KEY] = None
        st.session_state[QUEUE_VALIDATION_RESULTS_KEY] = None
        st.session_state[QUEUE_PREVIEW_SEEN_KEY] = False
        st.session_state[QUEUE_INPUT_SIGNATURE_KEY] = ""
    st.session_state[QUEUE_LAST_MODE_KEY] = mode

    uploaded_files: list[Any] | None = None
    pasted_text: str = ""
    if mode == QUEUE_INPUT_MODE_UPLOAD:
        uploaded_files = st.file_uploader(
            QUEUE_FILE_UPLOADER_LABEL,
            type=["json"],
            accept_multiple_files=True,
            key=QUEUE_UPLOADED_FILES_KEY,
            help="Drop one or more OSDU manifest JSON files.",
        )
    else:
        pasted_text = st.text_area(
            QUEUE_PASTE_TEXTAREA_LABEL,
            key=QUEUE_PASTE_TEXT_KEY,
            height=200,
            help="Paste many manifests, separated by a line containing only `---`.",
        )

    parse_clicked = st.button(
        QUEUE_PARSE_BUTTON_LABEL,
        key="queue_parse_btn",
        help="Parse and validate the queue without submitting.",
    )
    if parse_clicked:
        files_in: list[Any] | None
        if uploaded_files is None:
            files_in = None
        elif isinstance(uploaded_files, list):
            files_in = uploaded_files
        else:
            files_in = [uploaded_files]
        _handle_parse_queue(mode, files_in, pasted_text)

    items: list[QueueItem] | None = st.session_state.get(QUEUE_PARSED_ITEMS_KEY)
    validations: list[QueueValidationResult] | None = st.session_state.get(
        QUEUE_VALIDATION_RESULTS_KEY
    )

    # ----- Preview + cap UX -----
    cap_exceeded = False
    if items:
        valid_count = sum(1 for v in (validations or []) if v.ok)
        invalid_count = len(items) - valid_count
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Parsed", len(items))
        col_b.metric("Valid", valid_count)
        col_c.metric("Invalid", invalid_count)

        if len(items) > MAX_QUEUE_SIZE:
            cap_exceeded = True
            st.error(
                f"Queue exceeds the {MAX_QUEUE_SIZE}-row hard cap "
                f"({len(items)} parsed). Trim the input and re-parse."
            )
        elif len(items) >= 400:
            st.warning(
                f"Queue has {len(items)} rows — approaching the "
                f"{MAX_QUEUE_SIZE}-row cap. Consider splitting the batch."
            )

        # Show a compact preview table.
        rows = []
        for idx, item in enumerate(items, start=1):
            validation = (
                validations[idx - 1] if validations and idx - 1 < len(validations) else None
            )
            ok_flag = validation.ok if validation is not None else False
            err = validation.error_message if validation is not None else None
            rows.append(
                {
                    "#": idx,
                    "label": item.label,
                    "valid": "✅" if ok_flag else "❌",
                    "kinds": ", ".join(
                        validation.kinds if validation is not None else []
                    ),
                    "records": validation.record_count if validation else 0,
                    "error": (err or "")[:140],
                }
            )
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.caption("No queue parsed yet. Provide input above and click Parse.")

    st.markdown("---")
    st.subheader("Submission settings")

    # Load options autorun-once, with a manual refresh.
    _load_queue_input_options(connection)
    if st.button(
        QUEUE_REFRESH_OPTIONS_LABEL,
        key="queue_refresh_options_btn",
        help="Re-fetch legal tags and entitlement groups from ADME.",
    ):
        _load_queue_input_options(connection, force=True)

    _render_option_field(
        label="Legal tag",
        session_key=QUEUE_LEGAL_TAG_KEY,
        options=st.session_state.get(QUEUE_LEGAL_TAG_OPTIONS_KEY),
        placeholder="opendes-public-usa-dataset-1",
        help_text="Legal tag applied to every record in the batch.",
        empty_caption="Legal tag list unavailable — type the name manually.",
    )
    _render_option_field(
        label="ACL owners",
        session_key=QUEUE_ACL_OWNERS_KEY,
        options=st.session_state.get(QUEUE_ACL_OWNER_OPTIONS_KEY),
        placeholder="data.default.owners@example.com",
        help_text="Entitlement group with owner rights.",
        empty_caption="Entitlement groups unavailable — type the email manually.",
    )
    _render_option_field(
        label="ACL viewers",
        session_key=QUEUE_ACL_VIEWERS_KEY,
        options=st.session_state.get(QUEUE_ACL_VIEWER_OPTIONS_KEY),
        placeholder="data.default.viewers@example.com",
        help_text="Entitlement group with viewer rights.",
        empty_caption="Entitlement groups unavailable — type the email manually.",
    )

    inter_delay = st.number_input(
        "Pause between submits (seconds)",
        min_value=0.0,
        max_value=30.0,
        value=0.0,
        step=0.25,
        key=QUEUE_INTER_SUBMIT_DELAY_KEY,
        help="Throttle the queue to avoid hammering the ingestion endpoint.",
    )
    skip_invalid = st.checkbox(
        "Skip invalid rows (otherwise they are reported and skipped anyway)",
        value=True,
        key=QUEUE_SKIP_INVALID_KEY,
        help="Validation failures never block the rest of the queue.",
    )

    # Preview gate.
    preview_ok = st.checkbox(
        QUEUE_PREVIEW_CHECKBOX_LABEL,
        key=QUEUE_PREVIEW_SEEN_KEY,
        value=bool(st.session_state.get(QUEUE_PREVIEW_SEEN_KEY, False)),
        help="Required before submitting. Re-parsing clears this checkbox.",
    )

    legal_tag = str(st.session_state.get(QUEUE_LEGAL_TAG_KEY) or "").strip()
    acl_owners_raw = str(st.session_state.get(QUEUE_ACL_OWNERS_KEY) or "").strip()
    acl_viewers_raw = str(
        st.session_state.get(QUEUE_ACL_VIEWERS_KEY) or ""
    ).strip()
    acl_owners = [acl_owners_raw] if acl_owners_raw else []
    acl_viewers = [acl_viewers_raw] if acl_viewers_raw else []

    can_submit = bool(
        items
        and validations is not None
        and not cap_exceeded
        and preview_ok
        and legal_tag
        and acl_owners
        and acl_viewers
        and not st.session_state.get(QUEUE_SUBMIT_IN_FLIGHT_KEY, False)
    )
    disabled_reason: str | None = None
    if not items:
        disabled_reason = "Parse a queue before submitting."
    elif cap_exceeded:
        disabled_reason = (
            f"Queue exceeds the {MAX_QUEUE_SIZE}-row cap — trim and re-parse."
        )
    elif not preview_ok:
        disabled_reason = "Check the preview-confirmation box to unlock Submit."
    elif not legal_tag:
        disabled_reason = "Pick a legal tag."
    elif not acl_owners or not acl_viewers:
        disabled_reason = "Pick ACL owners and viewers."

    col_submit, col_abort = st.columns(2)
    with col_submit:
        submit_clicked = st.button(
            QUEUE_SUBMIT_BUTTON_LABEL,
            key="queue_submit_btn",
            disabled=not can_submit,
            on_click=_reset_queue_abort_and_mark_in_flight,
            help="Send every valid row through the bulk-ingestion service.",
        )
    with col_abort:
        st.button(
            QUEUE_ABORT_BUTTON_LABEL,
            key="queue_abort_btn",
            on_click=_set_queue_abort,
            help="Stop after the row currently in flight finishes.",
        )

    if disabled_reason is not None and not can_submit:
        st.caption(f"⏸️ {disabled_reason}")

    if submit_clicked and can_submit and items and validations is not None:
        # Fresh batch — clear prior live results.
        st.session_state[QUEUE_LIVE_RESULTS_KEY] = []
        _handle_submit_queue(
            connection,
            items=items,
            validations=validations,
            acl_owners=acl_owners,
            acl_viewers=acl_viewers,
            legal_tag=legal_tag,
            skip_invalid=bool(skip_invalid),
            inter_submit_delay=float(inter_delay),
            start_index=0,
        )

    # ----- Breaker banner + resume -----
    breaker_event = st.session_state.get(QUEUE_BREAKER_EVENT_KEY)
    if breaker_event is not None:
        st.error(
            "🛑 Circuit breaker tripped — "
            f"{breaker_event.threshold} consecutive failures detected. "
            f"Remaining rows skipped: {breaker_event.remaining_count}. "
            "Inspect the failed rows below before resuming."
        )
        resume_index = len(
            st.session_state.get(QUEUE_LIVE_RESULTS_KEY) or []
        )
        if items and resume_index < len(items):
            resume_clicked = st.button(
                QUEUE_RESUME_BUTTON_LABEL,
                key="queue_resume_btn",
                help=(
                    f"Resume the queue starting at row {resume_index + 1} "
                    "using the previous submission settings."
                ),
            )
            if resume_clicked and validations is not None:
                st.session_state[QUEUE_BREAKER_EVENT_KEY] = None
                st.session_state[QUEUE_ABORT_KEY] = False
                _handle_submit_queue(
                    connection,
                    items=items,
                    validations=validations,
                    acl_owners=acl_owners,
                    acl_viewers=acl_viewers,
                    legal_tag=legal_tag,
                    skip_invalid=bool(skip_invalid),
                    inter_submit_delay=float(inter_delay),
                    start_index=resume_index,
                )

    # ----- Live progress / post-batch summary -----
    live_results: list[QueueSubmitResult] = list(
        st.session_state.get(QUEUE_LIVE_RESULTS_KEY) or []
    )
    if live_results:
        st.markdown("---")
        st.subheader("Submission progress")
        _render_queue_progress_board(
            live_results,
            dict(st.session_state.get(QUEUE_LIVE_ATTEMPTS_KEY) or {}),
            total=len(items) if items else len(live_results),
        )

    summary = st.session_state.get(QUEUE_LAST_BATCH_SUMMARY_KEY)
    if summary:
        if summary.get("aborted"):
            st.warning(
                f"⏹ Batch aborted by operator after "
                f"{summary['completed_count']} of {summary['planned_total']} rows."
            )
        elif summary.get("failed", 0) == 0 and summary.get("skipped", 0) == 0:
            st.success(
                f"✅ All {summary['succeeded']} rows submitted successfully."
            )
        else:
            st.warning(
                f"{summary['succeeded']} of {summary['total']} succeeded — "
                f"{summary['failed']} failed, {summary['skipped']} skipped."
            )

        # Download failed-rows JSON.
        payload = _build_failed_rows_payload(
            live_results, connection.data_partition_id
        )
        if payload != b"[]":
            st.download_button(
                QUEUE_DOWNLOAD_FAILED_LABEL,
                data=payload,
                file_name="failed-queue-rows.json",
                mime="application/json",
                key="queue_download_failed_btn",
            )


if __name__ == "__main__":
    main()
