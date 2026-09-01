"""Semantic Connection setup blade.

Configures the Azure AI Search + Microsoft Foundry connection that powers
Intelligent Discovery (semantic search, the multi-entity catalog, and grounded
answer synthesis). This is the single place to enter these settings; the
Intelligent Discovery page reads them and checks this connection as a pre-req.

Keys are held in session only and never written to disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if PROJECT_ROOT not in {Path(path or ".").resolve() for path in sys.path}:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # type: ignore[import-not-found]  # noqa: E402

from app.semantic_connection import (  # noqa: E402
    SemanticSettings,
    get_semantic_settings,
    set_semantic_settings,
)
from app.services.marker_search import MarkerSearchError, search_markers  # noqa: E402
from app.services.semantic_catalog import search_catalog  # noqa: E402

DISCOVERY_PAGE_PATH = "pages/14_🔮_Discovery.py"
TEST_KEY = "semantic_connection_test"


def _run_smart_check(settings: SemanticSettings) -> list[tuple[str, bool, str]]:
    """Exercise each configured index/model; return (label, ok, detail)."""
    checks: list[tuple[str, bool, str]] = []

    try:
        hits = search_markers(settings.marker_config(), "test", top=1)
        checks.append(
            ("Marker index + embeddings", True, f"{len(hits)} result(s)")
        )
    except MarkerSearchError as exc:
        checks.append(("Marker index + embeddings", False, str(exc)))

    try:
        hits = search_catalog(settings.catalog_config(), "test", top=1)
        checks.append(("Catalog index", True, f"{len(hits)} result(s)"))
    except MarkerSearchError as exc:
        checks.append(("Catalog index", False, str(exc)))

    if settings.has_chat():
        try:
            from openai import OpenAI  # noqa: PLC0415

            from app.services.semantic_embeddings import OpenAIEmbedding

            client = OpenAI(
                base_url=OpenAIEmbedding._azure_base_url(settings.foundry_endpoint),
                api_key=settings.foundry_key,
            )
            client.chat.completions.create(
                model=settings.chat_deployment,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            checks.append(("Chat deployment", True, settings.chat_deployment))
        except Exception as exc:  # noqa: BLE001 - surface any failure
            checks.append(("Chat deployment", False, str(exc)))

    return checks


def main() -> None:
    """Render the Semantic Connection setup page."""
    st.set_page_config(
        page_title="Semantic Connection · ADME Control Plane",
        page_icon="🧠",
        layout="wide",
    )
    st.title("🧠 Semantic Connection")
    st.markdown(
        "Configure the **Azure AI Search** and **Microsoft Foundry** connection "
        "that powers **Intelligent Discovery** — semantic search, the "
        "multi-entity catalog, and grounded answers. Keys are held in session "
        "only and never written to disk."
    )

    current = get_semantic_settings(st.session_state)

    with st.form("semantic_connection_form"):
        st.subheader("Azure AI Search")
        search_endpoint = st.text_input(
            "Search endpoint", value=current.search_endpoint
        )
        col_a, col_b = st.columns(2)
        marker_index = col_a.text_input("Marker index", value=current.marker_index)
        catalog_index = col_b.text_input(
            "Catalog index", value=current.catalog_index
        )
        search_key = st.text_input(
            "Search admin/query key",
            value=current.search_key,
            type="password",
            help="Held in session only; never saved to disk.",
        )

        st.subheader("Microsoft Foundry")
        foundry_endpoint = st.text_input(
            "Foundry endpoint", value=current.foundry_endpoint
        )
        col_c, col_d = st.columns(2)
        deployment = col_c.text_input(
            "Embedding deployment", value=current.deployment
        )
        chat_deployment = col_d.text_input(
            "Chat deployment (optional)",
            value=current.chat_deployment,
            help=(
                "A Foundry chat model (e.g. gpt-4.1-mini) for fluent answer "
                "synthesis. Leave blank to use the grounded composed answer."
            ),
        )
        foundry_key = st.text_input(
            "Foundry key",
            value=current.foundry_key,
            type="password",
            help="Held in session only; never saved to disk.",
        )

        col_save, col_test = st.columns(2)
        saved = col_save.form_submit_button("Save connection", type="primary")
        tested = col_test.form_submit_button("Save & test connection")

    if saved or tested:
        settings = SemanticSettings(
            search_endpoint=search_endpoint.strip(),
            search_key=search_key.strip(),
            foundry_endpoint=foundry_endpoint.strip(),
            foundry_key=foundry_key.strip(),
            marker_index=marker_index.strip(),
            catalog_index=catalog_index.strip(),
            deployment=deployment.strip(),
            chat_deployment=chat_deployment.strip(),
        )
        set_semantic_settings(st.session_state, settings)
        current = settings
        if tested:
            if not settings.is_complete():
                st.session_state[TEST_KEY] = None
            else:
                with st.spinner("Testing connection…"):
                    st.session_state[TEST_KEY] = _run_smart_check(settings)

    st.divider()
    if current.is_complete():
        st.success("✅ Semantic connection is configured.")
        st.caption(
            f"Search `{current.search_endpoint}` · marker `{current.marker_index}`"
            f" · catalog `{current.catalog_index}` · Foundry "
            f"`{current.deployment}`"
            + (
                f" · chat `{current.chat_deployment}`"
                if current.has_chat()
                else " · no chat model (grounded answers)"
            )
        )
        st.page_link(
            DISCOVERY_PAGE_PATH,
            label="Go to Intelligent Discovery",
            icon="🔮",
        )
    else:
        missing = ", ".join(current.missing_fields())
        st.warning(f"Not configured yet. Missing: {missing}.")

    checks = st.session_state.get(TEST_KEY)
    if checks:
        st.subheader("Connection test")
        for label, ok, detail in checks:
            icon = "✅" if ok else "❌"
            st.markdown(f"{icon} **{label}** — {detail}")


if __name__ == "__main__":
    main()
