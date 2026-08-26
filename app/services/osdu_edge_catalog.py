"""Extract the OSDU relationship-edge catalog from JSON schemas.

This reuses the *core rule* of the Accenture OSDU-Ontology generator: an edge
(object property) exists wherever a schema property whose name ends in ``ID``
or ``IDs`` carries an ``x-osdu-relationship`` annotation naming the target
``EntityType``. The Accenture generator turns that into a ``has<EntityType>``
OWL object property; here we capture it as a plain edge definition so we can
(a) know which record fields are graph edges and (b) check which are actually
populated in the data.

Pure functions over already-loaded schema dicts — no network, no rdflib — so
the catalog is unit-testable and reusable by the graph-building POC.

Known limitation (documented as a gap): cross-file ``$ref`` inheritance is not
resolved here. We scan each schema file's own body, which captures the direct
relationship fields on the priority kinds (e.g. ``WellboreID`` → Wellbore,
``WellID`` → Well) but not relationships defined only in referenced abstract
schemas.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EdgeDef:
    """One relationship edge defined by a schema."""

    property_name: str
    target_entity_types: tuple[str, ...]
    is_array: bool = False


@dataclass
class KindEdges:
    """All relationship edges discovered for a single OSDU kind/schema."""

    schema_key: str
    class_name: str
    edges: list[EdgeDef] = field(default_factory=list)


def _entity_types(relationship: object) -> tuple[str, ...]:
    """Pull the EntityType values from an ``x-osdu-relationship`` list."""
    if not isinstance(relationship, list):
        return ()
    types: list[str] = []
    for entry in relationship:
        if isinstance(entry, dict):
            et = entry.get("EntityType")
            if isinstance(et, str) and et:
                types.append(et)
    return tuple(types)


def extract_relationship_edges(schema: dict) -> list[EdgeDef]:
    """Return all relationship edges declared anywhere in a schema body.

    Walks the schema recursively. Whenever it finds a property whose name ends
    in ``ID``/``IDs`` and whose definition contains a non-empty
    ``x-osdu-relationship`` with an ``EntityType``, it records an edge. Array
    relationship fields (``type: array`` with the annotation on the items) are
    flagged ``is_array=True`` so callers know the edge is one-to-many.
    """
    found: dict[str, EdgeDef] = {}

    def visit(node: object, prop_name: str | None) -> None:
        if isinstance(node, dict):
            # Is this node itself a relationship property definition?
            if prop_name and (prop_name.endswith("ID") or prop_name.endswith("IDs")):
                rel = node.get("x-osdu-relationship")
                targets = _entity_types(rel)
                if not targets and node.get("type") == "array":
                    items = node.get("items")
                    if isinstance(items, dict):
                        targets = _entity_types(items.get("x-osdu-relationship"))
                if targets:
                    is_array = node.get("type") == "array" or prop_name.endswith("IDs")
                    found[prop_name] = EdgeDef(
                        property_name=prop_name,
                        target_entity_types=targets,
                        is_array=is_array,
                    )
            # Recurse into "properties" with the child key as the property name.
            props = node.get("properties")
            if isinstance(props, dict):
                for key, value in props.items():
                    visit(value, key)
            # Recurse into structural keywords without changing the prop name.
            for keyword in ("allOf", "oneOf", "anyOf"):
                for entry in node.get(keyword, []) or []:
                    visit(entry, prop_name)
            items = node.get("items")
            if isinstance(items, dict):
                visit(items, prop_name)
        elif isinstance(node, list):
            for entry in node:
                visit(entry, prop_name)

    visit(schema, None)
    return list(found.values())


def class_name_from_schema(schema: dict, schema_key: str) -> str:
    """Best-effort class name: schema ``title`` or the file stem."""
    title = schema.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return schema_key
