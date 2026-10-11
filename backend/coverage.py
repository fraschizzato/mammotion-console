"""Estimated swept area from completed mowing segments, never from remaining paths."""

import math

from shapely.geometry import LineString, mapping
from shapely.ops import transform, unary_union


def completed_segments(
    full: dict, remaining: dict, active_hash: int, index: int, ordered: list
) -> dict:
    """Use the mower's ordered hash list as evidence that earlier segments finished."""
    features = []
    previous = set(ordered[: ordered.index(active_hash)]) if active_hash in ordered else set()
    active_remaining = {
        f.get("properties", {}).get("path_hash"): f.get("geometry", {}).get("coordinates", [])
        for f in remaining.get("features", [])
    }
    for feature in full.get("features", []):
        props = feature.get("properties", {})
        geometry = feature.get("geometry", {})
        path_hash = props.get("path_hash")
        if geometry.get("type") != "LineString" or props.get("path_type") not in (0, 2):
            continue
        coords = geometry["coordinates"]
        if path_hash in previous:
            passed = coords
        elif path_hash == active_hash and index > 0:
            passed = coords[: min(index, len(coords))]
            tail = active_remaining.get(path_hash)
            if tail:
                passed = passed + [tail[0]]
        else:
            continue
        if len(passed) >= 2:
            features.append(
                {
                    "type": "Feature",
                    "properties": {"path_hash": path_hash, "kind": "completed_path"},
                    "geometry": {"type": "LineString", "coordinates": passed},
                }
            )
    return {"type": "FeatureCollection", "features": features}


def swept_area(completed: dict, width_m: float) -> dict:
    """Local metric projection; width is reported lane spacing, so area is an estimate."""
    empty = {"type": "FeatureCollection", "features": []}
    if not math.isfinite(width_m) or not 0.05 <= width_m <= 1:
        return empty
    lines = []
    for feature in completed.get("features", []):
        geometry = feature.get("geometry", {})
        coords = geometry.get("coordinates", [])
        if geometry.get("type") == "LineString" and 2 <= len(coords) <= 100000:
            if all(
                len(p) >= 2
                and math.isfinite(p[0])
                and math.isfinite(p[1])
                and abs(p[0]) <= 180
                and abs(p[1]) < 85
                for p in coords
            ):
                lines.append(LineString(coords))
    if not lines:
        return empty
    origin = lines[0].coords[0]
    sx, sy = 111320 * math.cos(math.radians(origin[1])), 111320

    def project(x, y, z=None):
        return (x - origin[0]) * sx, (y - origin[1]) * sy

    def inverse(x, y, z=None):
        return x / sx + origin[0], y / sy + origin[1]

    area = unary_union(
        [transform(project, line).buffer(width_m / 2, cap_style="flat") for line in lines]
    )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "kind": "mowed_area",
                    "estimated": True,
                    "area_m2": round(area.area, 2),
                    "width_m": width_m,
                },
                "geometry": mapping(transform(inverse, area)),
            }
        ],
    }
