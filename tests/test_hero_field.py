"""Tests for the Hero Field synthetic dataset generator.

These assert the *coherence guarantees* that make the data demo-quality:
relationship ids resolve, the sidetrack references its parent, markers use the
Hugin/Draupne vocabulary, the report flags the hazard, and DQ/JV/lineage
metadata is present. No network I/O.
"""

from __future__ import annotations

from app.services.hero_field import (
    FIELD_NAME,
    build_hero_field,
)


def _records(manifest: dict) -> list[dict]:
    return manifest.get("MasterData", [])


def test_counts_match_summary() -> None:
    ds = build_hero_field()
    counts = ds.summary["counts"]
    assert len(_records(ds.manifests["load_Well.json"])) == counts["wells"]
    assert len(_records(ds.manifests["load_Wellbore.json"])) == counts["wellbores"]
    assert len(_records(ds.manifests["load_Organisation.json"])) == (
        counts["organisations"]
    )


def test_wellbore_wellid_links_resolve_to_wells() -> None:
    ds = build_hero_field()
    well_ids = {r["id"] for r in _records(ds.manifests["load_Well.json"])}
    for wb in _records(ds.manifests["load_Wellbore.json"]):
        ref = wb["data"]["WellID"]  # <namespace>:master-data--Well:AUR-01:
        key = ref.split(":")[-2]
        assert f"osdu:master-data--Well:{key}" in well_ids


def test_sidetrack_references_parent_wellbore() -> None:
    ds = build_hero_field()
    wellbores = {r["id"]: r for r in _records(ds.manifests["load_Wellbore.json"])}
    st_id = "osdu:master-data--Wellbore:AUR-01-ST1"
    assert st_id in wellbores
    st = wellbores[st_id]
    parent_ref = st["data"]["ParentWellboreID"]
    parent_key = parent_ref.split(":")[-2]
    assert f"osdu:master-data--Wellbore:{parent_key}" in wellbores
    assert st["data"]["SequenceNumber"] == 1
    assert st["tags"]["hero:sidetrack-of"] == "AUR-01-01"


def test_wells_form_spatial_cluster_with_coordinates() -> None:
    ds = build_hero_field()
    lons, lats = [], []
    for well in _records(ds.manifests["load_Well.json"]):
        coords = well["data"]["SpatialLocation"]["Wgs84Coordinates"]
        point = coords["features"][0]["geometry"]["coordinates"]
        lons.append(point[0])
        lats.append(point[1])
    # A tight cluster: spread under ~0.2 deg in each axis (a few km).
    assert max(lons) - min(lons) < 0.2
    assert max(lats) - min(lats) < 0.2


def test_markers_use_reservoir_and_seal_vocabulary() -> None:
    ds = build_hero_field()
    wpcs = _records(ds.manifests["load_WellboreMarkerSet.json"])
    assert wpcs, "expected marker records"
    for wpc in wpcs:
        names = {m["MarkerName"] for m in wpc["data"]["Markers"]}
        assert "Hugin Formation" in names
        assert "Draupne Formation" in names
        # WellboreID must point at an existing primary wellbore.
        assert wpc["data"]["WellboreID"].endswith("-01:")


def test_marker_depths_increase_downward() -> None:
    ds = build_hero_field()
    wpc = _records(ds.manifests["load_WellboreMarkerSet.json"])[0]
    depths = [m["MarkerMeasuredDepth"] for m in wpc["data"]["Markers"]]
    assert depths == sorted(depths)


def test_report_flags_hazard_and_links_hero_wellbore() -> None:
    ds = build_hero_field()
    document = _records(ds.manifests["load_Document.json"])[0]
    assert document["data"]["WellboreID"].endswith("AUR-01-01:")
    assert "shallow-gas" in document["tags"]["hero:hazard"]
    narrative = ds.documents["AUR-01_Final_Well_Report.txt"]
    assert "SHALLOW GAS" in narrative
    assert "SIDETRACK" in narrative


def test_jv_and_quality_metadata_present() -> None:
    ds = build_hero_field()
    wells = {r["id"]: r for r in _records(ds.manifests["load_Well.json"])}
    jv = wells["osdu:master-data--Well:AUR-05"]
    assert jv["tags"]["entitlement:jv"] == "true"
    assert jv["tags"]["entitlement:visibility"] == "metadata-only"
    flagged = wells["osdu:master-data--Well:AUR-03"]
    assert flagged["tags"]["quality:state"] == "Flagged"
    # Every well carries lineage/provenance.
    for well in wells.values():
        assert well["tags"]["lineage:source"]
        assert well["tags"]["hero:field"] == FIELD_NAME
