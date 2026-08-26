"""Well Graph page — visualize the OSDU relationship graph for a well.

Materializes the well package read-only from ADME Search (following explicit
`WellID`/`WellboreID` relationships) and renders it as an interactive graph,
plus provenance-labeled node/edge tables. This is the graph counterpart to the
semantic Marker Search page.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if PROJECT_ROOT not in {Path(path or ".").resolve() for path in sys.path}:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # type: ignore[import-untyped]  # noqa: E402
import pydeck as pdk  # type: ignore[import-untyped]  # noqa: E402
import streamlit as st  # type: ignore[import-not-found]  # noqa: E402
import streamlit.components.v1 as components  # type: ignore[import-not-found]  # noqa: E402

from app.connection_state import (  # noqa: E402
    ensure_session_defaults,
    get_connection,
    get_user_auth_state,
)
from app.services.auth import AuthenticationError, get_token  # noqa: E402
from app.services.instance_graph import (  # noqa: E402
    ROLE_COLORS,
    build_well_graph,
    to_graphviz_dot,
)
from app.services.spatial_search import (  # noqa: E402
    NearbyWell,
    nearby_wells,
    well_point,
)

try:
    from app.services.graph_viz import well_graph_to_vis_html  # noqa: E402

    _VIS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    _VIS_AVAILABLE = False

SETTINGS_PAGE_PATH = "pages/1_⚙️_Instance_Configuration.py"
WELL_TAIL_KEY = "well_graph_tail"
GRAPH_KEY = "well_graph_obj"
WELL_ID_KEY = "well_graph_well_id"
NEARBY_KEY = "well_graph_nearby"
POINT_KEY = "well_graph_point"

# Example well identifiers (the segment after ``master-data--Well:``).
EXAMPLES = {
    "TNO 2149 (2 wellbores)": "2149",
    "TNO 3174": "3174",
    "Volve 15/9-F-12 (shell)": "15%2F9-F-12",
}


def _set_example(tail: str) -> None:
    st.session_state[WELL_TAIL_KEY] = tail


def _edges_dataframe(graph) -> pd.DataFrame:
    rows = [
        {
            "From": e.source.split(":")[-1] or e.source,
            "Edge": e.type,
            "To": e.target.split(":")[-2] if e.target.endswith(":") else e.target.split(":")[-1],
            "Provenance": e.provenance,
        }
        for e in graph.edges
    ]
    return pd.DataFrame(rows)


def _role_counts(graph) -> pd.DataFrame:
    counts: dict[str, int] = {}
    for node in graph.nodes:
        counts[node.role] = counts.get(node.role, 0) + 1
    return pd.DataFrame(
        [{"Role": r, "Count": c} for r, c in sorted(counts.items())]
    )


def _nearby_map_df(
    ref_id: str, ref_lat: float, ref_lon: float, neighbors: list[NearbyWell]
) -> pd.DataFrame:
    rows = [
        {
            "id": ref_id,
            "label": ref_id.split(":")[-1] or ref_id,
            "dist_txt": "reference well",
            "lat": ref_lat,
            "lon": ref_lon,
            "color": [214, 39, 40],
            "radius": 220.0,
        }
    ]
    for n in neighbors:
        rows.append(
            {
                "id": n.id,
                "label": n.id.split(":")[-1] or n.id,
                "dist_txt": f"{n.distance_km} km away",
                "lat": n.latitude,
                "lon": n.longitude,
                "color": [31, 119, 180],
                "radius": 120.0,
            }
        )
    return pd.DataFrame(rows)


def _selected_well_id(event: object) -> str | None:
    """Extract the clicked well's record id from a pydeck selection event."""
    selection = getattr(event, "selection", None)
    if selection is None and isinstance(event, dict):
        selection = event.get("selection")
    if not selection:
        return None
    objects = (
        selection.get("objects") if hasattr(selection, "get") else None
    ) or {}
    rows = objects.get("wells") or []
    if rows and isinstance(rows[0], dict):
        return rows[0].get("id")
    return None


def _render_nearby_map(
    ref_id: str, ref_lat: float, ref_lon: float, neighbors: list[NearbyWell]
) -> str | None:
    """Render an interactive pydeck map with tooltips; return clicked well id."""
    df = _nearby_map_df(ref_id, ref_lat, ref_lon, neighbors)
    layer = pdk.Layer(
        "ScatterplotLayer",
        id="wells",
        data=df,
        get_position=["lon", "lat"],
        get_fill_color="color",
        get_radius="radius",
        radius_min_pixels=6,
        radius_max_pixels=40,
        pickable=True,
        auto_highlight=True,
    )
    view_state = pdk.ViewState(latitude=ref_lat, longitude=ref_lon, zoom=8)
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=view_state,
        map_style="light",
        tooltip={
            "html": "<b>{label}</b><br/>{dist_txt}",
            "style": {"backgroundColor": "#262730", "color": "white"},
        },
    )
    event = st.pydeck_chart(
        deck,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-object",
        key="nearby_deck",
    )
    return _selected_well_id(event)


def _render_nearby(connection, well_id: str) -> None:
    """Interactive spatial-neighborhood panel: radius slider + clickable map."""
    st.subheader("Nearby wells (spatial)")
    st.caption(
        "Geographically close wells via the OSDU byDistance spatial filter — "
        "hover for the well name, click a marker to act on it."
    )
    radius = st.slider("Radius (km)", min_value=1, max_value=50, value=10)
    find = st.button("Find nearby wells")

    if find:
        try:
            token = get_token(connection, get_user_auth_state(st.session_state))
        except AuthenticationError as exc:
            st.error(f"Authentication failed: {exc}")
            return
        with st.spinner("Searching nearby wells…"):
            point = well_point(connection, token, well_id)
            if point is None:
                st.session_state[NEARBY_KEY] = "no-point"
            else:
                lat, lon = point
                st.session_state[POINT_KEY] = point
                st.session_state[NEARBY_KEY] = nearby_wells(
                    connection,
                    token,
                    lat,
                    lon,
                    distance_km=float(radius),
                    limit=100,
                    exclude_id=well_id,
                )

    result = st.session_state.get(NEARBY_KEY)
    if result == "no-point":
        st.info(
            "This well has no WGS84 coordinates (e.g. a Volve shell), so a spatial "
            "search isn't possible for it."
        )
        return
    if not result:
        return

    point = st.session_state.get(POINT_KEY)
    neighbors: list[NearbyWell] = result
    st.caption(f"Found {len(neighbors)} well(s) within the radius.")

    selected_id: str | None = None
    if point:
        selected_id = _render_nearby_map(well_id, point[0], point[1], neighbors)

    if selected_id and selected_id != well_id:
        short = selected_id.split(":")[-1]
        st.success(f"Selected well **{short}**")
        if st.button(f"Build graph for {short}", key="pivot_to_selected"):
            try:
                token = get_token(
                    connection, get_user_auth_state(st.session_state)
                )
            except AuthenticationError as exc:
                st.error(f"Authentication failed: {exc}")
                return
            with st.spinner("Traversing relationships…"):
                st.session_state[GRAPH_KEY] = build_well_graph(
                    connection, token, selected_id, label="Well"
                )
            st.session_state[WELL_ID_KEY] = selected_id
            st.session_state[NEARBY_KEY] = None
            st.rerun()

    table = pd.DataFrame(
        [
            {"Well": n.id.split(":")[-1], "Distance (km)": n.distance_km}
            for n in neighbors
        ]
    )
    st.dataframe(table, width="stretch", hide_index=True)


def main() -> None:
    st.set_page_config(
        page_title="Graph · ADME Control Plane",
        page_icon="🕸️",
        layout="wide",
    )
    st.title("🕸️ Graph")
    st.markdown(
        "Materializes a well's relationship graph from ADME by following explicit "
        "`WellID` and `WellboreID` links (one-to-many, provenance-labeled). This is "
        "the graph view that complements semantic Marker Search."
    )

    ensure_session_defaults(st.session_state)
    connection = get_connection(st.session_state)
    if connection is None:
        st.warning("No ADME connection configured for this session.")
        st.page_link(SETTINGS_PAGE_PATH, label="Open Instance Configuration", icon="⚙️")
        return

    st.caption(
        f"Partition: `{connection.data_partition_id}` · Endpoint: `{connection.endpoint}`"
    )

    st.caption("Try an example:")
    columns = st.columns(len(EXAMPLES))
    for column, (label, tail) in zip(columns, EXAMPLES.items()):
        column.button(label, on_click=_set_example, args=(tail,), width="stretch")

    tail = st.text_input(
        "Well identifier (segment after `master-data--Well:`)",
        key=WELL_TAIL_KEY,
        placeholder="e.g. 2149",
    )
    run = st.button("Build graph", type="primary")

    if run:
        if not tail.strip():
            st.error("Enter a well identifier.")
            return
        well_id = f"{connection.data_partition_id}:master-data--Well:{tail.strip()}"
        try:
            token = get_token(connection, get_user_auth_state(st.session_state))
        except AuthenticationError as exc:
            st.error(f"Authentication failed: {exc}")
            return
        with st.spinner("Traversing relationships…"):
            graph = build_well_graph(connection, token, well_id, label="Well")
        st.session_state[GRAPH_KEY] = graph
        st.session_state[WELL_ID_KEY] = well_id
        st.session_state[NEARBY_KEY] = None  # reset spatial results for new well

    graph = st.session_state.get(GRAPH_KEY)
    well_id = st.session_state.get(WELL_ID_KEY)
    if graph is None or well_id is None:
        return

    wellbores = sum(1 for n in graph.nodes if n.role == "Wellbore")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Nodes", len(graph.nodes))
    col2.metric("Edges", len(graph.edges))
    col3.metric("Wellbores", wellbores)
    col4.metric("API calls", graph.api_calls)

    if len(graph.nodes) <= 1:
        st.info(
            "No related records found for this well. Its relationship fields may be "
            "unpopulated (e.g. Volve shells), or the identifier may be incorrect."
        )
    else:
        if _VIS_AVAILABLE:
            try:
                components.html(
                    well_graph_to_vis_html(graph, height=560),
                    height=580,
                    scrolling=False,
                )
            except Exception:  # noqa: BLE001 - fall back to static rendering
                st.graphviz_chart(to_graphviz_dot(graph), use_container_width=True)
        else:
            st.graphviz_chart(to_graphviz_dot(graph), use_container_width=True)
        legend = "  ".join(
            f"🟦{r}" if r == "Well" else f"● {r}" for r in ROLE_COLORS
        )
        st.caption(f"Roles: {legend}")

        left, right = st.columns([2, 1])
        with left:
            st.subheader("Edges")
            st.dataframe(_edges_dataframe(graph), width="stretch", hide_index=True)
        with right:
            st.subheader("Node roles")
            st.dataframe(_role_counts(graph), width="stretch", hide_index=True)

    st.divider()
    _render_nearby(connection, well_id)


if __name__ == "__main__":
    main()
