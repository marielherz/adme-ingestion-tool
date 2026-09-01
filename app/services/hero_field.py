"""Hero Field synthetic data generator (Hero 1 — sidetrack / graph story).

Produces a small, hand-crafted, *domain-coherent* OSDU dataset with the
"right answers" baked in — the opposite of a random field-filler. It models
one North Sea field ("Aurelia") whose stratigraphy reuses the Volve/Hugin
vocabulary (Hugin reservoir, Draupne seal, Heather, Sleipner, Skagerrak) so
semantic search and the graph light up with real geology.

What it encodes for the Hero 1 (sidetrack) story:
- A well cluster with **offset analogs** (spatial neighbours) plus a **dry hole**.
- A **hero well** (AUR-01) with a **sidetrack** wellbore (AUR-01-ST1) that
  references its parent wellbore — the graph "load all related data" beat.
- Coherent **WellID / WellboreID / parent-wellbore** links so the instance
  graph traverses cleanly.
- **WellboreMarkerSet** components per wellbore (Hugin/Draupne/... picks) so the
  marker vocabulary and Discovery experience have real formations to match.
- A **final well report** WorkProduct + Document whose narrative **flags a
  drilling hazard** (shallow gas + fault-related total losses that forced the
  sidetrack) and links to the hero wellbore.
- **Data-quality** state, **lineage/provenance**, and **JV entitlement**
  variation carried on record ``tags`` (schema-safe, no invented kinds).

All output is **local manifest JSON** — this module performs no network I/O and
never writes to ADME. References use the ``<namespace>`` placeholder and empty
acl/legal arrays, matching the existing TNO manifests so the bulk loader can
stamp partition, ACL, and legal tags at submit time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NAMESPACE = "<namespace>"
MANIFEST_KIND = "osdu:wks:Manifest:1.0.0"

WELL_KIND = "osdu:wks:master-data--Well:1.0.0"
WELLBORE_KIND = "osdu:wks:master-data--Wellbore:1.0.0"
ORG_KIND = "osdu:wks:master-data--Organisation:1.0.0"
MARKERSET_KIND = "osdu:wks:work-product-component--WellboreMarkerSet:1.0.0"
DOCUMENT_KIND = "osdu:wks:work-product-component--Document:1.0.0"

FIELD_NAME = "Aurelia"


def _ref(kind_path: str, key: str) -> str:
    """OSDU relationship reference, e.g. ``<namespace>:master-data--Well:AUR-01:``."""
    return f"{NAMESPACE}:{kind_path}:{key}:"


def _record_id(kind_path: str, key: str) -> str:
    """Manifest record id, e.g. ``osdu:master-data--Well:AUR-01``."""
    return f"osdu:{kind_path}:{key}"


def _empty_acl_legal() -> dict[str, Any]:
    return {
        "acl": {"owners": [], "viewers": []},
        "legal": {"legaltags": [], "otherRelevantDataCountries": []},
    }


# ---------------------------------------------------------------------------
# Curated field definition — the "answer key"
# ---------------------------------------------------------------------------

# Stratigraphy (top-to-base) shared across the field; depths shift per well.
# Reuses Volve/Hugin vocabulary so it aligns with the AVA-IQ narrative domain.
_STRAT_COLUMN: list[dict[str, Any]] = [
    {"name": "Nordland Group", "age": "Cenozoic", "role": "overburden"},
    {"name": "Hordaland Group", "age": "Cenozoic", "role": "overburden"},
    {"name": "Shetland Group", "age": "Cretaceous", "role": "overburden"},
    {"name": "Draupne Formation", "age": "Late Jurassic", "role": "seal"},
    {"name": "Heather Formation", "age": "Middle Jurassic", "role": "seal"},
    {"name": "Hugin Formation", "age": "Middle Jurassic", "role": "reservoir"},
    {"name": "Sleipner Formation", "age": "Middle Jurassic", "role": "reservoir"},
    {"name": "Skagerrak Formation", "age": "Triassic", "role": "basement"},
]


@dataclass(frozen=True)
class WellSpec:
    key: str
    name: str
    lon: float
    lat: float
    role: str  # discovery | appraisal | jv-appraisal | dry-hole
    hugin_top_md: float  # controls per-well depth shifts
    quality_state: str  # Certified | Provisional | Flagged
    operator: str  # organisation key
    jv_restricted: bool = False
    note: str = ""


# A tight spatial cluster (~a few km apart) so byDistance finds them as analogs.
_WELLS: list[WellSpec] = [
    WellSpec(
        key="AUR-01",
        name="Aurelia Discovery 1",
        lon=1.900,
        lat=58.440,
        role="discovery",
        hugin_top_md=3620.0,
        quality_state="Certified",
        operator="AURELIA-ENERGY",
        note="Oil discovery in Hugin; total losses on a fault forced a sidetrack.",
    ),
    WellSpec(
        key="AUR-02",
        name="Aurelia Appraisal 2",
        lon=1.930,
        lat=58.452,
        role="appraisal",
        hugin_top_md=3585.0,
        quality_state="Certified",
        operator="AURELIA-ENERGY",
        note="Up-dip appraisal; confirmed oil-down-to below Hugin top.",
    ),
    WellSpec(
        key="AUR-03",
        name="Aurelia Appraisal 3",
        lon=1.872,
        lat=58.421,
        role="appraisal",
        hugin_top_md=3675.0,
        quality_state="Flagged",
        operator="AURELIA-ENERGY",
        note="Directional survey failed QC (gyro drift); position provisional.",
    ),
    WellSpec(
        key="AUR-04",
        name="Aurelia Appraisal 4",
        lon=1.955,
        lat=58.418,
        role="appraisal",
        hugin_top_md=3640.0,
        quality_state="Certified",
        operator="AURELIA-ENERGY",
        note="Down-flank appraisal; thin but net-pay Hugin sands.",
    ),
    WellSpec(
        key="AUR-05",
        name="Aurelia JV Appraisal 5",
        lon=1.815,
        lat=58.470,
        role="jv-appraisal",
        hugin_top_md=3702.0,
        quality_state="Provisional",
        operator="NORDLYS-PARTNER",
        jv_restricted=True,
        note="JV-operated; metadata visible cross-partner, bulk data restricted.",
    ),
    WellSpec(
        key="AUR-06",
        name="Aurelia Exploration 6",
        lon=1.988,
        lat=58.401,
        role="dry-hole",
        hugin_top_md=3810.0,
        quality_state="Certified",
        operator="AURELIA-ENERGY",
        note="Dry hole; Hugin present but water-bearing below the fault block.",
    ),
]

# Organisations referenced by the wells.
_ORGS: list[dict[str, str]] = [
    {"key": "AURELIA-ENERGY", "name": "Aurelia Energy AS"},
    {"key": "NORDLYS-PARTNER", "name": "Nordlys Partner ASA"},
    {"key": "APEX-DATA", "name": "Apex Subsurface Data Services"},
]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _org_records() -> list[dict[str, Any]]:
    records = []
    for org in _ORGS:
        rec = {
            "id": _record_id("master-data--Organisation", org["key"]),
            "kind": ORG_KIND,
            **_empty_acl_legal(),
            "data": {
                "OrganisationName": org["name"],
                "Source": "Aurelia synthetic hero dataset",
            },
        }
        records.append(rec)
    return records


def _well_records() -> list[dict[str, Any]]:
    records = []
    for spec in _WELLS:
        tags = {
            "quality:state": spec.quality_state,
            "lineage:source": "Aurelia PSDM 2024 reprocessing",
            "lineage:curator": "Apex Subsurface Data Services",
            "hero:field": FIELD_NAME,
            "hero:role": spec.role,
        }
        if spec.jv_restricted:
            tags["entitlement:jv"] = "true"
            tags["entitlement:visibility"] = "metadata-only"

        rec = {
            "id": _record_id("master-data--Well", spec.key),
            "kind": WELL_KIND,
            **_empty_acl_legal(),
            "tags": tags,
            "data": {
                "FacilityName": spec.name,
                "FacilityID": spec.key,
                "Source": "Aurelia synthetic hero dataset",
                "ExistenceKind": _ref(
                    "reference-data--ExistenceKind", "Active"
                ),
                "CurrentOperatorID": _ref(
                    "master-data--Organisation", spec.operator
                ),
                "DataSourceOrganisationID": _ref(
                    "master-data--Organisation", "APEX-DATA"
                ),
                "SpatialLocation": {
                    "Wgs84Coordinates": {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {
                                    "type": "Point",
                                    "coordinates": [spec.lon, spec.lat],
                                },
                                "properties": {},
                            }
                        ],
                    }
                },
                "NameAliases": [
                    {
                        "AliasName": spec.name,
                        "AliasNameTypeID": _ref(
                            "reference-data--AliasNameType", "RegulatoryName"
                        ),
                    }
                ],
            },
        }
        records.append(rec)
    return records


def _wellbore_records() -> list[dict[str, Any]]:
    records = []
    for spec in _WELLS:
        primary_key = f"{spec.key}-01"
        records.append(
            _wellbore_record(
                key=primary_key,
                name=f"{spec.name} main bore",
                well_key=spec.key,
                sequence=0,
                spec=spec,
                trajectory="Directional",
                parent_wellbore_key=None,
            )
        )
        # Hero well gets a sidetrack that references its parent wellbore.
        if spec.role == "discovery":
            records.append(
                _wellbore_record(
                    key=f"{spec.key}-ST1",
                    name=f"{spec.name} sidetrack 1",
                    well_key=spec.key,
                    sequence=1,
                    spec=spec,
                    trajectory="Directional",
                    parent_wellbore_key=primary_key,
                    kickoff_note=(
                        "Sidetrack kicked off above the fault after total "
                        "circulation losses; geosteered back into Hugin."
                    ),
                )
            )
    return records


def _wellbore_record(
    *,
    key: str,
    name: str,
    well_key: str,
    sequence: int,
    spec: WellSpec,
    trajectory: str,
    parent_wellbore_key: str | None,
    kickoff_note: str = "",
) -> dict[str, Any]:
    tags = {
        "quality:state": spec.quality_state,
        "hero:field": FIELD_NAME,
        "hero:role": spec.role,
    }
    if parent_wellbore_key:
        tags["hero:sidetrack-of"] = parent_wellbore_key
    if spec.jv_restricted:
        tags["entitlement:jv"] = "true"
        tags["entitlement:visibility"] = "metadata-only"

    data: dict[str, Any] = {
        "FacilityName": name,
        "FacilityID": key,
        "WellID": _ref("master-data--Well", well_key),
        "SequenceNumber": sequence,
        "Source": "Aurelia synthetic hero dataset",
        "ExistenceKind": _ref("reference-data--ExistenceKind", "Active"),
        "CurrentOperatorID": _ref("master-data--Organisation", spec.operator),
        "DataSourceOrganisationID": _ref("master-data--Organisation", "APEX-DATA"),
        "TrajectoryTypeID": _ref(
            "reference-data--WellboreTrajectoryType", trajectory
        ),
        "NameAliases": [
            {
                "AliasName": name,
                "AliasNameTypeID": _ref(
                    "reference-data--AliasNameType", "RegulatoryName"
                ),
            }
        ],
    }
    if parent_wellbore_key:
        # Explicit parent-wellbore link so the graph shows the sidetrack lineage.
        data["ParentWellboreID"] = _ref(
            "master-data--Wellbore", parent_wellbore_key
        )
    if kickoff_note:
        data["Remark"] = kickoff_note

    return {
        "id": _record_id("master-data--Wellbore", key),
        "kind": WELLBORE_KIND,
        **_empty_acl_legal(),
        "tags": tags,
        "data": data,
    }


def _markers_for(spec: WellSpec) -> list[dict[str, Any]]:
    """Depth-stacked markers for a well, anchored on its Hugin top."""
    # Fixed vertical offsets (m) from Hugin top for each unit above/below.
    offsets = {
        "Nordland Group": -3200.0,
        "Hordaland Group": -2400.0,
        "Shetland Group": -900.0,
        "Draupne Formation": -180.0,
        "Heather Formation": -60.0,
        "Hugin Formation": 0.0,
        "Sleipner Formation": 85.0,
        "Skagerrak Formation": 190.0,
    }
    markers = []
    for unit in _STRAT_COLUMN:
        md = round(spec.hugin_top_md + offsets[unit["name"]], 1)
        markers.append(
            {
                "MarkerName": unit["name"],
                "MarkerMeasuredDepth": md,
                "GeologicalAge": unit["age"],
                "SurfaceRole": unit["role"],
            }
        )
    return markers


def _markerset_records() -> list[dict[str, Any]]:
    """A standalone WellboreMarkerSet record per (non-sidetrack) wellbore.

    Emitted as flat Storage records (real ids, no DAG surrogate keys) so they
    load through the same Storage API path as the master data and are picked
    up by the marker-vocabulary reindex and the instance graph.
    """
    records: list[dict[str, Any]] = []
    for spec in _WELLS:
        wellbore_key = f"{spec.key}-01"
        records.append(
            {
                "id": _record_id(
                    "work-product-component--WellboreMarkerSet",
                    f"{wellbore_key}-markers",
                ),
                "kind": MARKERSET_KIND,
                **_empty_acl_legal(),
                "tags": {
                    "quality:state": spec.quality_state,
                    "hero:field": FIELD_NAME,
                },
                "data": {
                    "Name": f"{spec.name} stratigraphic picks",
                    "Description": (
                        f"Formation tops for {spec.name} ({wellbore_key}): "
                        "Hugin reservoir sealed by Draupne, over Skagerrak."
                    ),
                    "WellboreID": _ref("master-data--Wellbore", wellbore_key),
                    "Markers": _markers_for(spec),
                },
            }
        )
    return records


# The hazard narrative that the Hero 1 "reports flag a hazard" beat surfaces.
REPORT_NARRATIVE = """\
AURELIA DISCOVERY 1 (AUR-01) — FINAL WELL REPORT
=================================================

SUMMARY
AUR-01 was drilled to appraise the Hugin Formation on the Aurelia structure.
The well confirmed an oil discovery in clean Hugin sandstones sealed by the
Draupne Formation claystone. A drilling hazard was encountered that materially
changed the well programme and is documented below.

DRILLING HAZARDS
1. SHALLOW GAS (approx. 470-490 m MD). A shallow gas sand was flagged on the
   site-survey and confirmed while drilling the 26" section. The interval was
   drilled with a weighted mud cap and the diverter armed. No influx reached
   surface. Offset wells AUR-02 and AUR-04 should treat 450-500 m as a
   shallow-gas hazard zone.

2. FAULT-RELATED TOTAL LOSSES (approx. 3540 m MD). While drilling toward the
   Hugin target the bit intersected a sub-seismic fault at the Heather level and
   the well took total circulation losses. Losses could not be cured with LCM.
   The lower hole was plugged back and a SIDETRACK (AUR-01-ST1) was kicked off
   above the fault and geosteered back into the Hugin reservoir, which was
   penetrated with good oil shows and net pay.

RESERVOIR
The Hugin Formation was encountered near 3620 m MD with an oil-water contact
consistent with AUR-02. The Draupne Formation provides an effective top seal.
The Skagerrak Formation forms the base of the prognosed section.

DATA QUALITY / LINEAGE
Positional data derived from the Aurelia PSDM 2024 reprocessing (curated by
Apex Subsurface Data Services). Directional surveys passed QC. This report is
the authoritative narrative source for the AUR-01 sidetrack decision.
"""


def _document_records() -> list[dict[str, Any]]:
    """A standalone Document WPC that flags the AUR-01 drilling hazard.

    The hazard narrative lives in ``data.Description`` (searchable) and, in
    full, in the sibling ``.txt`` under ``documents/``. Emitted as a flat
    Storage record so it loads with everything else.
    """
    return [
        {
            "id": _record_id(
                "work-product-component--Document", "AUR-01-final-report"
            ),
            "kind": DOCUMENT_KIND,
            **_empty_acl_legal(),
            "tags": {
                "quality:state": "Certified",
                "hero:field": FIELD_NAME,
                "hero:hazard": "shallow-gas;total-losses",
                "lineage:source": "Aurelia PSDM 2024 reprocessing",
            },
            "data": {
                "Name": "AUR-01 Final Well Report",
                "Description": (
                    "Final well report for AUR-01. Flags a shallow-gas hazard "
                    "(470-490 m) and fault-related total losses (~3540 m) that "
                    "forced the AUR-01-ST1 sidetrack back into the Hugin "
                    "reservoir. Confirms an oil discovery in the Hugin "
                    "Formation sealed by the Draupne Formation."
                ),
                "WellboreID": _ref("master-data--Wellbore", "AUR-01-01"),
            },
        }
    ]


def _master_manifest(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": MANIFEST_KIND, "MasterData": records}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class HeroFieldDataset:
    """All Hero Field manifests plus the report text and an answer-key summary."""

    manifests: dict[str, dict[str, Any]] = field(default_factory=dict)
    documents: dict[str, str] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def build_hero_field() -> HeroFieldDataset:
    """Build the coherent Hero 1 dataset in memory (no I/O)."""
    org_records = _org_records()
    well_records = _well_records()
    wellbore_records = _wellbore_records()

    manifests = {
        "load_Organisation.json": _master_manifest(org_records),
        "load_Well.json": _master_manifest(well_records),
        "load_Wellbore.json": _master_manifest(wellbore_records),
        "load_WellboreMarkerSet.json": _master_manifest(_markerset_records()),
        "load_Document.json": _master_manifest(_document_records()),
    }

    summary = {
        "field": FIELD_NAME,
        "story": "Hero 1 — sidetrack / graph",
        "counts": {
            "organisations": len(org_records),
            "wells": len(well_records),
            "wellbores": len(wellbore_records),
            "markersets": len(_WELLS),
            "reports": 1,
        },
        "hero_well": "AUR-01",
        "sidetrack": {
            "wellbore": "AUR-01-ST1",
            "parent": "AUR-01-01",
            "reason": "fault-related total losses",
        },
        "offset_analogs": [s.key for s in _WELLS if s.role == "appraisal"],
        "dry_hole": [s.key for s in _WELLS if s.role == "dry-hole"],
        "jv_restricted": [s.key for s in _WELLS if s.jv_restricted],
        "flagged_quality": [
            s.key for s in _WELLS if s.quality_state == "Flagged"
        ],
        "hazard_report": {
            "document": "AUR-01 Final Well Report",
            "wellbore": "AUR-01-01",
            "hazards": ["shallow gas (470-490 m)", "total losses (~3540 m)"],
        },
        "reservoir": "Hugin Formation",
        "seal": "Draupne Formation",
    }

    return HeroFieldDataset(
        manifests=manifests,
        documents={"AUR-01_Final_Well_Report.txt": REPORT_NARRATIVE},
        summary=summary,
    )
