"""Render a WellGraph as an interactive vis.js network (Kusto-style).

Produces a self-contained HTML string with a dark, force-directed graph:
nodes colored by role and sized by degree, curved labeled edges, drag/zoom,
and hover tooltips. Kept separate from :mod:`instance_graph` so the core graph
model has no visualization dependency.
"""

from __future__ import annotations

from app.services.instance_graph import ROLE_COLORS, WellGraph

# Physics + styling tuned for the "floaty network" look.
_VIS_OPTIONS = """
{
  "nodes": {
    "shape": "dot",
    "borderWidth": 2,
    "shadow": {"enabled": true, "size": 14, "color": "rgba(0,0,0,0.6)"},
    "font": {"color": "#e6e6e6", "size": 16, "face": "Segoe UI"}
  },
  "edges": {
    "color": {"color": "#6b7280", "highlight": "#c9d1d9", "opacity": 0.8},
    "smooth": {"type": "dynamic"},
    "arrows": {"to": {"enabled": true, "scaleFactor": 0.6}},
    "font": {"color": "#9aa4b2", "size": 11, "strokeWidth": 0, "align": "middle"},
    "width": 1.5
  },
  "interaction": {"hover": true, "tooltipDelay": 120, "navigationButtons": false},
  "physics": {
    "solver": "forceAtlas2Based",
    "forceAtlas2Based": {
      "gravitationalConstant": -60,
      "centralGravity": 0.012,
      "springLength": 130,
      "springConstant": 0.08,
      "damping": 0.55
    },
    "stabilization": {"enabled": true, "iterations": 220}
  }
}
"""


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _short_label(node_id: str) -> str:
    parts = node_id.split(":")
    if len(parts) >= 3:
        tail = parts[2] or (parts[3] if len(parts) > 3 else "")
        return tail[:16]
    return node_id[:16]


def well_graph_to_vis_html(graph: WellGraph, *, height: int = 600) -> str:
    """Return interactive vis.js HTML for a WellGraph (dark, force-directed)."""
    from pyvis.network import Network  # noqa: PLC0415 - optional viz dependency

    net = Network(
        height=f"{height}px",
        width="100%",
        bgcolor="#0e1117",
        font_color="#e6e6e6",
        directed=True,
        notebook=False,
        cdn_resources="remote",
    )
    net.set_options(_VIS_OPTIONS)

    # Degree drives node size (like Kusto's hub nodes).
    degree: dict[str, int] = {}
    for edge in graph.edges:
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1

    for node in graph.nodes:
        color = ROLE_COLORS.get(node.role, "#8892b0")
        size = 14 + 6 * degree.get(node.id, 0)
        size = min(size, 60)
        net.add_node(
            node.id,
            label=f"{node.role}\n{_short_label(node.id)}",
            title=f"{node.role}\n{node.id}",
            color={
                "background": color,
                "border": _hex_to_rgba(color, 0.9),
                "highlight": {"background": color, "border": "#ffffff"},
            },
            size=size,
        )

    for edge in graph.edges:
        net.add_edge(edge.source, edge.target, label=edge.type, title=edge.provenance)

    return net.generate_html(notebook=False)
