"""Persistent planned and actually traveled geometry for mower job history."""

import copy
import gzip
import json
import math
import os
import threading
from pathlib import Path
from uuid import UUID

from backend.coverage import swept_area
from backend.storage import now

LAYERS = ("path", "progress", "traveled", "mowed")


def empty_layer() -> dict:
    return {"type": "FeatureCollection", "features": []}


def _layer(value) -> dict:
    if not isinstance(value, dict) or value.get("type") != "FeatureCollection":
        return empty_layer()
    features = value.get("features")
    if not isinstance(features, list) or len(features) > 20000:
        return empty_layer()
    return {"type": "FeatureCollection", "features": copy.deepcopy(features)}


def _coordinates(feature: dict) -> list:
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates")
    return (
        coordinates
        if geometry.get("type") == "LineString" and isinstance(coordinates, list)
        else []
    )


def _prefix(left: list, right: list) -> bool:
    return len(left) <= len(right) and left == right[: len(left)]


def merge_traveled(previous: dict, current: dict) -> dict:
    """Retain prior MQTT segments while replacing their growing live version."""
    merged = _layer(previous).get("features", [])
    for candidate in _layer(current).get("features", []):
        coordinates = _coordinates(candidate)
        if not coordinates:
            if candidate not in merged:
                merged.append(candidate)
            continue
        source = (candidate.get("properties") or {}).get("source")
        replaced = False
        for index, existing in enumerate(merged):
            if (existing.get("properties") or {}).get("source") != source:
                continue
            old = _coordinates(existing)
            if old == coordinates or _prefix(coordinates, old):
                replaced = True
                break
            if _prefix(old, coordinates):
                merged[index] = candidate
                replaced = True
                break
        if not replaced:
            merged.append(candidate)
    return {"type": "FeatureCollection", "features": merged[-2000:]}


def _coverage_width(*layers: dict) -> float | None:
    for layer in layers:
        for feature in layer.get("features", []):
            raw_width = (feature.get("properties") or {}).get("width_m")
            if raw_width is None:
                continue
            try:
                width = float(raw_width)
            except (TypeError, ValueError):
                continue
            if math.isfinite(width) and 0.05 <= width <= 1:
                return width
    return None


def _summary(payload: dict) -> dict:
    def count(name: str) -> int:
        return len((payload.get(name) or {}).get("features", []))

    area = 0.0
    for feature in (payload.get("mowed") or {}).get("features", []):
        try:
            area += max(0, float((feature.get("properties") or {}).get("area_m2", 0)))
        except (TypeError, ValueError):
            pass
    points = sum(
        len(_coordinates(feature))
        for feature in (payload.get("traveled") or {}).get("features", [])
    )
    return {
        "available": any(count(name) for name in LAYERS),
        "updated": payload["updated"],
        "snapshot_id": payload.get("snapshot_id"),
        "planned_features": count("path"),
        "remaining_features": count("progress"),
        "traveled_features": count("traveled"),
        "traveled_points": points,
        "mowed_features": count("mowed"),
        "mowed_area_m2": round(area, 2),
    }


class JobRoutes:
    """Atomic gzip files keep large GeoJSON outside the SQLite document rows."""

    def __init__(self, root: Path):
        self.root = root.resolve() / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def path(self, key: str) -> Path:
        UUID(key)
        return self.root / f"{key}.geojson.json.gz"

    def get(self, key: str) -> dict:
        path = self.path(key)
        if not path.is_file():
            raise KeyError("Job route not found")
        with self.lock, gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)

    def save(
        self,
        key: str,
        overlay: dict,
        *,
        device: str,
        state: str,
        snapshot_id: str | None,
        position: dict | None = None,
    ) -> dict:
        with self.lock:
            try:
                previous = self.get(key)
            except KeyError:
                previous = {}
            current = {name: _layer(overlay.get(name)) for name in LAYERS}
            traveled = merge_traveled(previous.get("traveled", empty_layer()), current["traveled"])
            width = _coverage_width(current["mowed"], previous.get("mowed", empty_layer()))
            mowed = swept_area(traveled, width) if width and traveled["features"] else empty_layer()
            metadata = next(
                (
                    feature.get("properties") or {}
                    for layer in (current["mowed"], previous.get("mowed", empty_layer()))
                    for feature in layer.get("features", [])
                    if feature.get("properties")
                ),
                {},
            )
            for feature in mowed["features"]:
                measured = feature.get("properties") or {}
                feature["properties"] = {
                    key: value for key, value in metadata.items() if key != "area_m2"
                } | measured
            if not mowed["features"]:
                mowed = (
                    current["mowed"]
                    if current["mowed"]["features"]
                    else previous.get("mowed", empty_layer())
                )
            payload = {
                "schema_version": 1,
                "history_id": key,
                "device": device,
                "state": state,
                "started": previous.get("started", now()),
                "updated": now(),
                "snapshot_id": snapshot_id or previous.get("snapshot_id"),
                "last_position": position or previous.get("last_position"),
                "path": current["path"]
                if current["path"]["features"]
                else previous.get("path", empty_layer()),
                "progress": current["progress"]
                if current["progress"]["features"]
                else previous.get("progress", empty_layer()),
                "traveled": traveled,
                "mowed": mowed,
            }
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            path = self.path(key)
            if path.is_file():
                with gzip.open(path, "rb") as stream:
                    old = stream.read()
                # Ignore the observation timestamp when deciding whether a rewrite is useful.
                old_payload = json.loads(old)
                old_payload["updated"] = payload["updated"]
                if (
                    json.dumps(old_payload, ensure_ascii=False, separators=(",", ":")).encode()
                    == encoded
                ):
                    return _summary(json.loads(old))
            temporary = path.with_suffix(path.suffix + ".tmp")
            with gzip.open(temporary, "wb", compresslevel=6) as stream:
                stream.write(encoded)
            os.replace(temporary, path)
            return _summary(payload)

    def response(self, key: str) -> dict:
        payload = self.get(key)
        return {
            **{name: payload.get(name, empty_layer()) for name in LAYERS},
            "status": {
                "source": "job_history",
                "stale": True,
                "historical": True,
                "history_id": key,
                "updated": payload.get("updated"),
                "snapshot_id": payload.get("snapshot_id"),
                "snapshot_matches": True,
            },
            "last_position": payload.get("last_position"),
        }
