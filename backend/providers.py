import json
import math
from pathlib import Path

from backend.maps import MAX_BYTES


def map_evidence(raw: dict, geojson: dict, source: str) -> dict:
    areas = raw.get("area", {})
    expected = {str(x) for x in areas}
    # PyMammotion intentionally retains old area-name entries after a map edit.
    # Names without a corresponding current area are metadata orphans, not zones
    # that the immutable snapshot must invent or wait to download.
    complete = bool(areas)
    expected_static: set[str] = set()
    for kind in (
        "area",
        "obstacle",
        "path",
        "dump",
        "corridor_line",
        "corridor_point",
        "virtual_wall",
        "visual_safety_zone",
        "visual_obstacle_zone",
    ):
        for frame in raw.get(kind, {}).values():
            data = frame.get("data", [])
            complete &= bool(data) and len(data) == frame.get("total_frame")
            indexes = [x.get("current_frame") for x in data]
            complete &= set(indexes) == set(range(1, len(data) + 1))
        expected_static.update(str(x) for x in raw.get(kind, {}))
    for root in raw.get("root_hash_lists", []):
        if root.get("sub_cmd") != 0:
            continue
        complete &= len(root.get("data", [])) == root.get("total_frame")
        declared = {
            str(h) for frame in root.get("data", []) for h in frame.get("data_couple", []) if h
        }
        complete &= declared.issubset(expected_static)
    return {
        "geojson": geojson,
        "raw_map": raw,
        "expected_zone_ids": sorted(expected),
        "frames_complete": complete,
        "expected_static_ids": sorted(expected_static),
        "crs": "EPSG:4326",
        "source_metadata": {"provider": source},
    }


def local_map_geojson(raw: dict, location: dict) -> dict:
    """Build WGS84 geometry from PyMammotion's local map frames.

    PyMammotion stores the RTK origin in radians and the dock as local ENU
    metres.  Older HA cache fixtures used degrees, so the live device position
    is used to disambiguate those legacy values when it is available.
    """
    origin = location.get("RTK", {})
    lat, lon = origin.get("latitude"), origin.get("longitude")
    if lat is None or lon is None or (lat == 0 and lon == 0):
        raise ValueError("A valid RTK WGS84 origin is required to transform local map coordinates")
    lat, lon = _rtk_degrees(float(lat), float(lon), location.get("device", {}))
    if not (-85 < lat < 85 and -180 <= lon <= 180):
        raise ValueError("A valid RTK WGS84 origin is required to transform local map coordinates")
    yaw = origin.get("yaw", 0)
    names = {str(a["hash"]): a["name"] for a in raw.get("area_name", [])}
    features = []
    for kind in (
        "area",
        "obstacle",
        "path",
        "dump",
        "corridor_line",
        "corridor_point",
        "virtual_wall",
        "visual_safety_zone",
        "visual_obstacle_zone",
    ):
        for key, frames in raw.get(kind, {}).items():
            points = []
            for frame in sorted(frames.get("data", []), key=lambda x: x.get("current_frame", 0)):
                for point in frame.get("data_couple", []):
                    east = math.cos(yaw) * point["x"] - math.sin(yaw) * point["y"]
                    north = math.sin(yaw) * point["x"] + math.cos(yaw) * point["y"]
                    points.append(
                        [lon + east / (111320 * math.cos(math.radians(lat))), lat + north / 111320]
                    )
            if not points:
                continue
            if kind in {"area", "obstacle", "visual_safety_zone", "visual_obstacle_zone"}:
                if points[0] != points[-1]:
                    points.append(points[0])
                geometry = {"type": "Polygon", "coordinates": [points]}
            elif kind in {"dump", "corridor_point"}:
                geometry = {"type": "Point", "coordinates": points[0]}
            else:
                geometry = (
                    {"type": "LineString", "coordinates": points}
                    if len(points) > 1
                    else {"type": "Point", "coordinates": points[0]}
                )
            features.append(
                {
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {
                        "type_name": kind,
                        "hash": str(key),
                        "Name": names.get(str(key), kind),
                    },
                }
            )
    features.append(
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"kind": "rtk", "name": "RTK"},
        }
    )
    dock = location.get("dock", {})
    # Dock latitude/longitude are unfortunately named upstream: they are
    # East/North offsets in metres relative to the RTK, not WGS84 degrees.
    if dock.get("latitude") or dock.get("longitude"):
        dock_lat = lat + float(dock.get("longitude", 0)) / 111320
        dock_lon = lon + float(dock.get("latitude", 0)) / (111320 * math.cos(math.radians(lat)))
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [dock_lon, dock_lat]},
                "properties": {"kind": "dock", "name": "Dock"},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _rtk_degrees(latitude: float, longitude: float, device: dict) -> tuple[float, float]:
    """Return an RTK origin in degrees without guessing from magnitude alone."""
    radians_candidate = (math.degrees(latitude), math.degrees(longitude))
    device_lat = device.get("latitude")
    device_lon = device.get("longitude")
    if device_lat is not None and device_lon is not None:
        try:
            device_point = (float(device_lat), float(device_lon))
            raw_distance = math.hypot(latitude - device_point[0], longitude - device_point[1])
            radians_distance = math.hypot(
                radians_candidate[0] - device_point[0], radians_candidate[1] - device_point[1]
            )
            return radians_candidate if radians_distance < raw_distance else (latitude, longitude)
        except (TypeError, ValueError):
            pass
    # The current PyMammotion Location model explicitly stores RTK coordinates
    # in radians. Values outside the angular radian range are legacy degrees.
    if abs(latitude) <= math.pi / 2 and abs(longitude) <= math.pi:
        return radians_candidate
    return latitude, longitude


def merge_missing_map_features(generated: dict, rebuilt: dict) -> dict:
    """Keep SDK geometry and append raw map objects the SDK intentionally omitted."""
    features = list(generated.get("features", []))
    expected_types = {
        "area",
        "obstacle",
        "path",
        "dump",
        "corridor_line",
        "corridor_point",
        "virtual_wall",
        "visual_safety_zone",
        "visual_obstacle_zone",
    }
    present = {
        (str(props.get("type_name")), str(props.get("hash")))
        for feature in features
        if (props := feature.get("properties") or {}).get("type_name") in expected_types
    }
    for feature in rebuilt.get("features", []):
        props = feature.get("properties") or {}
        identity = (str(props.get("type_name")), str(props.get("hash")))
        if identity[0] in expected_types and identity not in present:
            features.append(feature)
            present.add(identity)
    return {"type": "FeatureCollection", "features": features}


class CacheSource:
    """Versioned read-only parser. Never opens auth/config-entry stores."""

    def __init__(self, root: Path | None):
        self.root = root.resolve() if root else None

    def devices(self) -> dict:
        if not self.root:
            return {}
        result = {}
        for path in (self.root / ".storage").glob("mammotion.*"):
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(self.root)
                or path.stat().st_size > MAX_BYTES
            ):
                continue
            try:
                payload = json.loads(path.read_bytes())
                if payload.get("version") != 1:
                    continue
                data = payload.get("data", {})
                devices = data.get("devices", {}) if payload.get("minor_version", 1) >= 2 else data
                for name, device in devices.items():
                    if isinstance(device, dict) and isinstance(device.get("map"), dict):
                        result[name] = device
            except (OSError, ValueError):
                continue
        return result

    def fetch(self, device_name: str) -> dict:
        device = self.devices().get(device_name)
        if not device:
            raise ValueError(
                "No matching Mammotion cache; verify the read-only mount and device identity"
            )
        raw = device["map"]
        geojson = raw.get("generated_geojson")
        if not geojson:
            geojson = local_map_geojson(raw, device.get("location", {}))
        payload = map_evidence(raw, geojson, "ha-cache-v1")
        payload["source_metadata"]["coordinate_origin"] = device.get("location", {}).get("RTK")
        payload["source_metadata"]["cache_warning"] = (
            "HA cache can lag five minutes; this is a saved map, not proof of the current cloud map"
        )
        return payload

    def versions(self) -> dict:
        if not self.root:
            return {}
        path = self.root / "custom_components/mammotion/manifest.json"
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            return {}
        try:
            data = json.loads(path.read_bytes())
            return {"mammotion": data.get("version"), "requirements": data.get("requirements", [])}
        except (OSError, ValueError):
            return {}
