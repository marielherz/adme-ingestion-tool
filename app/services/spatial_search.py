"""Spatial helpers for finding geographically nearby wells (read-only).

Wraps the OSDU ``byDistance`` spatial filter and parses WGS84 geometry out of
records so callers get simple ``(latitude, longitude)`` points. Geometry
parsing and the haversine distance are pure functions (unit-testable); the
network calls delegate to :mod:`app.services.search`.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

from app.models.connection import ADMEConnection
from app.services.search import search_by_distance, search_with_cursor

WELL_KIND = "osdu:wks:master-data--Well:1.0.0"
SPATIAL_FIELD = "data.SpatialLocation.Wgs84Coordinates"
_EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class NearbyWell:
    """A well returned by a spatial search, with its point and distance."""

    id: str
    latitude: float
    longitude: float
    distance_km: float


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = (
        sin(d_lat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))


def _first_coordinate(node: object) -> tuple[float, float] | None:
    """Descend a nested GeoJSON coordinates array to the first ``[lon, lat]``."""
    if isinstance(node, list) and node:
        if isinstance(node[0], (int, float)) and len(node) >= 2:
            return float(node[0]), float(node[1])
        return _first_coordinate(node[0])
    return None


def extract_lon_lat(geometry: object) -> tuple[float, float] | None:
    """Pull a representative ``(longitude, latitude)`` from OSDU WGS84 geometry.

    Handles GeometryCollection, FeatureCollection, Feature, and bare geometry
    shapes. Returns ``None`` when no coordinate can be found.
    """
    if not isinstance(geometry, dict):
        return None
    geometries = geometry.get("geometries")
    if isinstance(geometries, list) and geometries:
        return extract_lon_lat(geometries[0])
    features = geometry.get("features")
    if isinstance(features, list) and features:
        first = features[0]
        if isinstance(first, dict):
            return extract_lon_lat(first.get("geometry"))
    coords = geometry.get("coordinates")
    if coords is not None:
        return _first_coordinate(coords)
    return None


def _geometry_from_data(data: dict) -> object:
    """Read the WGS84 geometry from a record ``data`` dict (dotted or nested)."""
    dotted = data.get("SpatialLocation.Wgs84Coordinates")
    if dotted is not None:
        return dotted
    spatial = data.get("SpatialLocation")
    if isinstance(spatial, dict):
        return spatial.get("Wgs84Coordinates")
    return None


def well_point(
    connection: ADMEConnection, token: str, well_id: str
) -> tuple[float, float] | None:
    """Return ``(latitude, longitude)`` for a well, or ``None`` if unmapped."""
    page = search_with_cursor(
        connection,
        token,
        kind=WELL_KIND,
        query=f'id:"{well_id}"',
        limit=1,
        returned_fields=("id", SPATIAL_FIELD),
    )
    if not page.ok:
        return None
    results = (page.raw_response or {}).get("results", [])
    for record in results:
        data = record.get("data") if isinstance(record, dict) else None
        if isinstance(data, dict):
            lon_lat = extract_lon_lat(_geometry_from_data(data))
            if lon_lat is not None:
                return lon_lat[1], lon_lat[0]  # (lat, lon)
    return None


def nearby_wells(
    connection: ADMEConnection,
    token: str,
    latitude: float,
    longitude: float,
    *,
    distance_km: float,
    limit: int = 50,
    exclude_id: str | None = None,
) -> list[NearbyWell]:
    """Find wells within ``distance_km`` of a point, sorted nearest-first."""
    hits, _status, _err = search_by_distance(
        connection,
        token,
        kind=WELL_KIND,
        latitude=latitude,
        longitude=longitude,
        distance_m=distance_km * 1000.0,
        spatial_field=SPATIAL_FIELD,
        returned_fields=("id", SPATIAL_FIELD),
        limit=limit,
    )
    found: list[NearbyWell] = []
    for hit in hits:
        well_id = hit.get("id")
        if not well_id or well_id == exclude_id:
            continue
        data = hit.get("data") if isinstance(hit, dict) else None
        lon_lat = extract_lon_lat(_geometry_from_data(data)) if isinstance(data, dict) else None
        if lon_lat is None:
            continue
        lon, lat = lon_lat
        found.append(
            NearbyWell(
                id=well_id,
                latitude=lat,
                longitude=lon,
                distance_km=round(haversine_km(latitude, longitude, lat, lon), 2),
            )
        )
    found.sort(key=lambda w: w.distance_km)
    return found
