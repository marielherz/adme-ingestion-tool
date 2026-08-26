"""Discovery page — combined semantic + graph experience (GraphRAG).

A natural-language query is *interpreted semantically* against the marker
vocabulary index (meaning, not keywords), the top concepts resolve to *anchor
wells*, and each anchor well is *expanded into its relationship package* via the
instance graph. The result is one connected answer — concepts -> wells ->
wellbores -> components — with provenance, rather than isolated search hits.

Semantic config resolves from environment variables with sidebar overrides;
graph expansion uses the app's ADME connection. Read-only.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if PROJECT_ROOT not in {Path(path or ".").resolve() for path in sys.path}:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # type: ignore[import-untyped]  # noqa: E402
import streamlit as st  # type: ignore[import-not-found]  # noqa: E402
import streamlit.components.v1 as components  # type: ignore[import-not-found]  # noqa: E402

from app.connection_state import (  # noqa: E402
    ensure_session_defaults,
    get_connection,
    get_user_auth_state,
)
from app.semantic_connection import get_semantic_settings  # noqa: E402
from app.services.answer import DiscoveryAnswer, synthesize_answer  # noqa: E402
from app.services.auth import AuthenticationError, get_token  # noqa: E402
from app.services.discovery import (  # noqa: E402
    DiscoveryError,
    DiscoveryResult,
    expand_wells,
    interpret_unified,
    merge_discovery_graph,
)

try:
    from app.services.graph_viz import well_graph_to_vis_html  # noqa: E402

    _VIS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    _VIS_AVAILABLE = False

QUERY_KEY = "discovery_query_input"
RESULT_KEY = "discovery_result"
ANSWER_KEY = "discovery_answer"
ERROR_KEY = "discovery_error"
EXPAND_KEY = "discovery_expand_n"
GRAPH_CACHE_KEY = "discovery_graph_cache"
DEFAULT_EXPAND = 3
SEMANTIC_SETUP_PAGE_PATH = "pages/15_🧠_Semantic_Connection.py"

# Real-world questions spanning stratigraphy, drilling hazards, and reports.
EXAMPLE_QUESTIONS = [
    "Which wells hit a shallow gas drilling hazard?",
    "Show me the Hugin reservoir oil discovery and its sidetrack",
    "Wells with salt seals over sandstone reservoirs",
    "Which wells took total losses on a fault?",
    "Find the final well report for the Aurelia field",
    "Chalk and limestone formations",
]


def _set_example(text: str) -> None:
    st.session_state[QUERY_KEY] = text


def _concepts_dataframe(result: DiscoveryResult) -> pd.DataFrame:
    rows = []
    for hit in result.concepts:
        anchored = result.concept_wells.get(hit.marker_name, [])
        rows.append(
            {
                "Score": round(hit.rank_score, 2),
                "Concept": hit.marker_name,
                "Geological age": ", ".join(hit.geological_ages) or "—",
                "Wells": hit.wellbore_count,
                "Anchored wells": ", ".join(w.split(":")[-1] for w in anchored) or "—",
            }
        )
    return pd.DataFrame(rows)


def _package_dataframe(result: DiscoveryResult) -> pd.DataFrame:
    rows = []
    for graph in result.graphs:
        counts: dict[str, int] = {}
        for node in graph.nodes:
            counts[node.role] = counts.get(node.role, 0) + 1
        rows.append(
            {
                "Anchor well": graph.well_id.split(":")[-1],
                "Wellbores": counts.get("Wellbore", 0),
                "Trajectories": counts.get("WellboreTrajectory", 0),
                "Logs": counts.get("WellLog", 0),
                "Marker sets": counts.get("WellboreMarkerSet", 0),
                "Interval sets": counts.get("WellboreIntervalSet", 0),
                "Nodes": len(graph.nodes),
            }
        )
    return pd.DataFrame(rows)


def _catalog_dataframe(result: DiscoveryResult) -> pd.DataFrame:
    rows = []
    for hit in result.catalog_hits:
        anchored = result.concept_wells.get(hit.title, [])
        snippet = " ".join(hit.content.split())
        if len(snippet) > 140:
            snippet = snippet[:140] + "…"
        rows.append(
            {
                "Score": round(hit.rank_score, 2),
                "Source": hit.source,
                "Title": hit.title,
                "Summary": snippet,
                "Anchored wells": ", ".join(w.split(":")[-1] for w in anchored)
                or "—",
            }
        )
    return pd.DataFrame(rows)


def _get_token(connection):
    """Fetch an ADME token, storing any auth error in session; return or None."""
    try:
        return get_token(connection, get_user_auth_state(st.session_state))
    except AuthenticationError as exc:
        st.session_state[ERROR_KEY] = f"Authentication failed: {exc}"
        return None


def _expand_cached(connection, anchor_well_ids, n):
    """Expand the first ``n`` anchor wells, caching graphs per ``n`` in session."""
    cache = st.session_state.setdefault(GRAPH_CACHE_KEY, {})
    if n in cache:
        return cache[n]
    token = _get_token(connection)
    if token is None:
        return None
    graphs, calls = expand_wells(connection, token, anchor_well_ids[:n])
    cache[n] = (graphs, calls)
    return graphs, calls


def main() -> None:
    """Render the Intelligent Discovery page."""
    st.set_page_config(
        page_title="Intelligent Discovery · ADME Control Plane",
        page_icon="🔮",
        layout="wide",
    )
    ensure_session_defaults(st.session_state)

    st.title("🔮 Intelligent Discovery")
    st.markdown(
        "Ask a question in plain language. Discovery **interprets** it across "
        "the subsurface knowledge base — stratigraphy, wells, and report "
        "narratives — finds the **anchor wells**, then the graph **expands** "
        "them into their full relationship package. One connected answer, with "
        "provenance."
    )

    connection = get_connection(st.session_state)
    settings = get_semantic_settings(st.session_state)

    if settings.is_complete():
        chat_note = (
            f"chat model `{settings.chat_deployment}`"
            if settings.has_chat()
            else "grounded answers (no chat model)"
        )
        st.success(f"✅ Semantic connection enabled — {chat_note}.")
    else:
        st.warning(
            "**Semantic connection required.** Intelligent Discovery needs the "
            "Azure AI Search + Foundry connection configured first."
        )
        st.page_link(
            SEMANTIC_SETUP_PAGE_PATH,
            label="Set up Semantic Connection",
            icon="🧠",
        )
        return

    marker_config = settings.marker_config()
    catalog_config = settings.catalog_config()
    chat_deployment = settings.chat_deployment

    st.caption("Try a question:")
    for row_start in range(0, len(EXAMPLE_QUESTIONS), 3):
        row = EXAMPLE_QUESTIONS[row_start : row_start + 3]
        columns = st.columns(len(row))
        for column, example in zip(columns, row):
            column.button(
                example,
                key=f"example_{example}",
                on_click=_set_example,
                args=(example,),
                width="stretch",
            )

    query_text = st.text_input(
        "Ask about the subsurface",
        key=QUERY_KEY,
        placeholder="e.g. which wells hit a shallow gas hazard near a fault?",
    )
    top_results = st.slider(
        "Results to return",
        3,
        15,
        6,
        help="How many semantic matches to consider from each source.",
    )
    run = st.button("Discover", type="primary")

    if run:
        st.session_state[ERROR_KEY] = None
        st.session_state[RESULT_KEY] = None
        st.session_state.pop(GRAPH_CACHE_KEY, None)
        st.session_state.pop(EXPAND_KEY, None)
        if connection is None:
            st.session_state[ERROR_KEY] = (
                "No ADME connection is configured. Set it on the Instance "
                "Configuration page first."
            )
        elif not query_text.strip():
            st.session_state[ERROR_KEY] = "Enter a question."
        else:
            token = _get_token(connection)
            if token is not None:
                with st.spinner("Interpreting…"):
                    try:
                        result = interpret_unified(
                            marker_config,
                            catalog_config,
                            connection,
                            token,
                            query_text,
                            top_results=top_results,
                        )
                        st.session_state[RESULT_KEY] = result
                        st.session_state[ANSWER_KEY] = synthesize_answer(
                            result,
                            chat_deployment=chat_deployment or None,
                            foundry_endpoint=marker_config.foundry_endpoint,
                            foundry_key=marker_config.foundry_key,
                        )
                    except DiscoveryError as exc:
                        st.session_state[ERROR_KEY] = str(exc)

    error = st.session_state.get(ERROR_KEY)
    if error:
        st.error(error)

    result: DiscoveryResult | None = st.session_state.get(RESULT_KEY)
    if result is None:
        return

    if not result.concepts and not result.catalog_hits:
        st.info("Nothing matched. Try rephrasing the question.")
        return

    answer: DiscoveryAnswer | None = st.session_state.get(ANSWER_KEY)
    if answer is not None:
        st.subheader("Answer")
        st.markdown(answer.text)
        if answer.citations:
            with st.container():
                st.caption("Sources")
                for cite in answer.citations[:8]:
                    st.markdown(
                        f"- **{cite.label}** · `{cite.record_id}`  \n"
                        f"  <span style='color:#8892b0'>{cite.source}"
                        f"{' — ' + cite.detail if cite.detail else ''}</span>",
                        unsafe_allow_html=True,
                    )
        badge = "generated by chat model" if answer.generated_by == "llm" else (
            "grounded summary (no model) — set a chat deployment for fuller prose"
        )
        st.caption(badge)

    with st.expander("Supporting records (evidence)", expanded=False):
        if result.catalog_hits:
            st.caption("Records matched by meaning — reports, wells, wellbores.")
            st.dataframe(
                _catalog_dataframe(result), width="stretch", hide_index=True
            )
        if result.concepts:
            st.caption("Stratigraphic concepts matched by meaning.")
            st.dataframe(
                _concepts_dataframe(result), width="stretch", hide_index=True
            )

    anchors = result.anchor_well_ids
    if not anchors:
        st.warning(
            "Matched, but nothing resolved to a parent well (missing WellID). "
            "Nothing to expand."
        )
        return

    # Expand a subset of the anchor wells into the graph. The control lives
    # *below* the graph so the user tunes expansion against what they see.
    if EXPAND_KEY not in st.session_state:
        st.session_state[EXPAND_KEY] = min(DEFAULT_EXPAND, len(anchors))
    n = min(st.session_state[EXPAND_KEY], len(anchors))
    expanded = _expand_cached(connection, anchors, n)
    if expanded is None:
        st.error(st.session_state.get(ERROR_KEY, "Could not expand wells."))
        return
    graphs, expand_calls = expanded
    display = replace(result, graphs=graphs)

    st.subheader("Connected graph")
    metric_cols = st.columns(3)
    metric_cols[0].metric("Anchor wells found", len(anchors))
    metric_cols[1].metric("Wells expanded", len(graphs))
    metric_cols[2].metric("Graph nodes", display.total_nodes)
    st.caption(
        "Anchor wells expanded into their relationship packages. Yellow "
        "**Concept** nodes bridge the semantic match to the graph."
    )
    merged = merge_discovery_graph(display)
    if _VIS_AVAILABLE:
        components.html(well_graph_to_vis_html(merged, height=620), height=640)
    else:
        st.info("Install pyvis to see the interactive network.")

    if len(anchors) > 1:
        st.slider(
            "Wells to expand",
            1,
            len(anchors),
            key=EXPAND_KEY,
            help=(
                "Expand more of the matched anchor wells into the graph above."
            ),
        )
        st.caption(
            f"Showing {len(graphs)} of {len(anchors)} matched anchor wells. "
            "Drag to expand more."
        )

    st.subheader("Package summary")
    st.dataframe(
        _package_dataframe(display),
        width="stretch",
        hide_index=True,
    )
    st.caption(
        f"Assembled from {result.api_calls + expand_calls} read-only ADME "
        "search call(s). Relationships followed via explicit OSDU "
        "WellID/WellboreID edges."
    )



if __name__ == "__main__":
    main()
