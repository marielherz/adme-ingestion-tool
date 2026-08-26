"""Tests for spatial geometry parsing and haversine distance."""

from __future__ import annotations

from app.services.spatial_search import (
    extract_lon_lat,
    haversine_km,
)


def test_extract_from_geometry_collection() -> None:
    geom = {
        "type": "geometrycollection",
        "geometries": [{"type": "point", "coordinates": [6.92013149, 52.31978743]}],
    }
    assert extract_lon_lat(geom) == (6.92013149, 52.31978743)


def test_extract_from_feature_collection() -> None:
    geom = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [3.1, 51.9]}}
        ],
    }
    assert extract_lon_lat(geom) == (3.1, 51.9)


def test_extract_from_polygon_takes_first_vertex() -> None:
    geom = {"type": "Polygon", "coordinates": [[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]]}
    assert extract_lon_lat(geom) == (1.0, 2.0)


def test_extract_returns_none_for_empty() -> None:
    assert extract_lon_lat({}) is None
    assert extract_lon_lat(None) is None


def test_haversine_known_distance() -> None:
    # ~157 km between two points 1 degree of latitude apart at the equator... use
    # a stable check: same point is 0, and 1 deg lat ~= 111 km.
    assert haversine_km(52.0, 6.0, 52.0, 6.0) == 0.0
    d = haversine_km(52.0, 6.0, 53.0, 6.0)
    assert 110.0 < d < 112.0
