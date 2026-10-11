import gzip
import hashlib
import io
import json
import math
import os
import shutil
import threading
import zipfile
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from shapely import normalize as canonical_geometry
from shapely.errors import ShapelyError
from shapely.geometry import mapping, shape
from shapely.ops import transform

from backend.config import sanitize
from backend.storage import Store, now

MAX_BYTES = 32 * 1024 * 1024
STATIC_TYPES = {
    "area",
    "obstacle",
    "path",
    "dump",
    "corridor_line",
    "corridor_point",
    "virtual_wall",
    "visual_safety_zone",
    "visual_obstacle_zone",
    "rtk",
    "dock",
    "svg",
    "zone",
    "no_go",
    "channel",
}


class MapFeature(BaseModel):
    type: Literal["Feature"]
    geometry: dict
    properties: dict | None = Field(default_factory=dict)
    id: str | int | None = None


class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"]
    features: list[MapFeature] = Field(min_length=1, max_length=10000)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def geometry_fingerprint(value: dict) -> str:
    """Return the stable fingerprint used by archived map features."""
    geometry = shape(value)
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("Invalid map geometry")
    normalized = json.loads(canonical(mapping(canonical_geometry(geometry))))
    return hashlib.sha256(canonical(normalized).encode()).hexdigest()


def geometry_area_m2(value: dict) -> float:
    """Measure a small WGS84 polygon in a local metric projection."""
    geometry = shape(value)
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("Invalid map geometry")
    west, south, east, north = geometry.bounds
    origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
    sx = 111320 * math.cos(math.radians(origin_lat))

    def project(x: float, y: float, z: float | None = None):
        return ((x - origin_lon) * sx, (y - origin_lat) * 111320)

    projected = transform(
        project,
        geometry,
    )
    return float(projected.area)


def normalize(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Map payload must be a JSON object")
    geo = payload.get("geojson", payload)
    if not isinstance(geo, dict):
        raise ValueError("GeoJSON must be an object")
    FeatureCollection.model_validate(geo)
    if geo.get("type") != "FeatureCollection" or payload.get("crs", "EPSG:4326") != "EPSG:4326":
        raise ValueError(
            "A WGS84 FeatureCollection is required; unreferenced local XY is not accepted"
        )
    features = []
    for item in geo.get("features", []):
        props = item.get("properties") or {}
        kind = str(props.get("kind", props.get("type_name", "zone"))).lower()
        if kind == "station":
            kind = (
                "rtk" if "rtk" in str(props.get("Name", props.get("title", ""))).lower() else "dock"
            )
        if kind not in STATIC_TYPES:
            continue
        if kind in {"rtk", "dock"} and item.get("geometry", {}).get("coordinates") == [0, 0]:
            continue
        try:
            geometry = shape(item["geometry"])
        except (TypeError, KeyError, ValueError, IndexError, ShapelyError) as exc:
            raise ValueError("Invalid map geometry structure") from exc
        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type
            not in {
                "Point",
                "MultiPoint",
                "LineString",
                "MultiLineString",
                "Polygon",
                "MultiPolygon",
            }
        ):
            raise ValueError("Invalid or unsupported map geometry")
        geometry_bounds = geometry.bounds
        if (
            not all(math.isfinite(x) for x in geometry_bounds)
            or geometry_bounds[0] < -180
            or geometry_bounds[2] > 180
            or geometry_bounds[1] < -90
            or geometry_bounds[3] > 90
        ):
            raise ValueError("Coordinates must be finite WGS84 longitude/latitude")
        source_id = str(props.get("source_id", props.get("hash", item.get("id", ""))))
        normalized = json.loads(canonical(mapping(canonical_geometry(geometry))))
        geometry_hash = geometry_fingerprint(normalized)
        features.append(
            {
                "type": "Feature",
                "geometry": normalized,
                "properties": {
                    "kind": "zone" if kind == "area" else kind,
                    "source_id": source_id,
                    "name": str(
                        props.get("name", props.get("Name", props.get("title", kind.title())))
                    )[:200],
                    "geometry_fingerprint": geometry_hash,
                },
            }
        )
    zones = [
        f
        for f in features
        if f["properties"]["kind"] == "zone"
        and f["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    ]
    if not zones:
        raise ValueError("No static work zones found; mowing progress is not a complete base map")
    features.sort(key=canonical)
    expected = {str(x) for x in payload.get("expected_zone_ids", [])}
    actual = {f["properties"]["source_id"] for f in zones}
    if "" in actual or len(actual) != len(zones):
        raise ValueError("Every zone requires a unique nonempty source ID")
    complete = "complete" if expected and expected == actual else "uncertain"
    if expected and expected != actual:
        complete = "partial"
    if payload.get("frames_complete") is False:
        complete = "partial"
    expected_static = {str(x) for x in payload.get("expected_static_ids", [])}
    actual_static = {f["properties"]["source_id"] for f in features}
    if expected_static and not expected_static.issubset(actual_static):
        complete = "partial"
    if payload.get("sync_verified") is False and complete != "partial":
        complete = "uncertain"
    bounds = [
        min(shape(f["geometry"]).bounds[0] for f in features),
        min(shape(f["geometry"]).bounds[1] for f in features),
        max(shape(f["geometry"]).bounds[2] for f in features),
        max(shape(f["geometry"]).bounds[3] for f in features),
    ]
    result = {"type": "FeatureCollection", "features": features}
    fingerprint = hashlib.sha256(canonical(result).encode()).hexdigest()
    return {
        "geojson": result,
        "fingerprint": fingerprint,
        "completeness": complete,
        "bounds": bounds,
        "zones": [{**f["properties"], "local_id": str(uuid4())} for f in zones],
        "crs": "EPSG:4326",
    }


def same_static_map(left: dict, right: dict) -> bool:
    def index(geo):
        return {
            (f["properties"]["kind"], f["properties"]["source_id"], f["properties"]["name"]): f
            for f in geo["features"]
        }

    a, b = index(left), index(right)
    if len(a) != len(left["features"]) or len(b) != len(right["features"]) or a.keys() != b.keys():
        return False
    for key in a:
        x, y = a[key]["geometry"], b[key]["geometry"]
        if x == y:
            continue
        if key[0] not in {"dock", "rtk"} or x["type"] != "Point" or y["type"] != "Point":
            return False
        lon, lat = x["coordinates"][:2]
        other_lon, other_lat = y["coordinates"][:2]
        distance = math.hypot(
            (other_lon - lon) * 111320 * math.cos(math.radians(lat)), (other_lat - lat) * 111320
        )
        if distance > 0.02:
            return False
    return True


def preview(geo: dict, bounds: list) -> str:
    x0, y0, x1, y1 = bounds
    scale = min(280 / max(x1 - x0, 1e-9), 160 / max(y1 - y0, 1e-9))
    paths = []
    for feature in geo["features"]:
        geometry = feature["geometry"]
        rings = geometry["coordinates"] if geometry["type"] == "Polygon" else []
        for ring in rings:
            points = " ".join(
                f"{10 + (x - x0) * scale:.2f},{170 - (y - y0) * scale:.2f}" for x, y, *_ in ring
            )
            color = "#a5e47a" if feature["properties"]["kind"] == "zone" else "#ef9672"
            paths.append(
                f'<polygon points="{points}" stroke="{color}" fill="{color}" fill-opacity=".18" stroke-width="2"/>'
            )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 180"><rect width="300" height="180" fill="#14221d"/>'
        + "".join(paths)
        + "</svg>"
    )


class MapArchive:
    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.RLock()

    def list(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM snapshots ORDER BY created DESC").fetchall()
        return [dict(r) | {"manifest": json.loads(r["manifest"])} for r in rows]

    def zone_names_by_geometry(self) -> dict[str, str]:
        """Use the newest archived human name for an unchanged zone geometry."""
        result: dict[str, str] = {}
        for snapshot in self.list():
            for zone in snapshot["manifest"].get("zones", []):
                fingerprint = str(zone.get("geometry_fingerprint", ""))
                name = str(zone.get("name", "")).strip()
                generic = name.casefold().startswith(("zone ", "area "))
                if fingerprint and name and not generic:
                    result.setdefault(fingerprint, name)
        return result

    def get(self, key: str):
        UUID(key)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM snapshots WHERE id=?", (key,)).fetchone()
        result = dict(row) | {"manifest": json.loads(row["manifest"])} if row else None
        if not result:
            raise KeyError("Snapshot not found")
        return result

    def directory(self, key: str):
        self.get(key)
        return self.store.root / "maps" / key

    def capture(
        self,
        payload: dict,
        device: str,
        name: str,
        set_id: str | None = None,
        force: bool = False,
        source: str = "import",
    ):
        normalized = normalize(payload)
        if normalized["completeness"] != "complete":
            raise ValueError(
                f"Map is {normalized['completeness']}; expected zone manifest and complete geometry are required"
            )
        with self.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM snapshots WHERE device=? AND fingerprint=? ORDER BY created LIMIT 1",
                (device, normalized["fingerprint"]),
            ).fetchone()
            if existing and not force:
                return self.get(existing["id"]) | {"unchanged": True}
            if not force:
                # Dock/RTK fixes can jitter by millimetres between restarts.
                # Compare with immutable originals, without rounding saved geometry.
                for row in db.execute(
                    "SELECT id FROM snapshots WHERE device=? ORDER BY created DESC", (device,)
                ):
                    previous = json.loads(
                        (self.store.root / "maps" / row["id"] / "normalized.geojson").read_text()
                    )
                    if same_static_map(previous, normalized["geojson"]):
                        return self.get(row["id"]) | {"unchanged": True}
            if set_id:
                owner = db.execute(
                    "SELECT device FROM snapshots WHERE set_id=? LIMIT 1", (set_id,)
                ).fetchone()
                if not owner or owner["device"] != device:
                    raise ValueError("Map set must exist and belong to the selected mower")
            set_id = set_id or str(uuid4())
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM snapshots WHERE set_id=?", (set_id,)
            ).fetchone()[0]
            key, created = str(uuid4()), now()
            manifest = {k: v for k, v in normalized.items() if k != "geojson"}
            manifest |= {
                "schema_version": 1,
                "id": key,
                "device": device,
                "set_id": set_id,
                "version": version,
                "created": created,
                "source": source,
                "name": name,
                "source_metadata": sanitize(payload.get("source_metadata", {})),
            }
            temporary = self.store.root / "maps" / f".pending-{key}"
            final = self.store.root / "maps" / key
            temporary.mkdir()
            try:
                files = {
                    "raw.json.gz": gzip.compress(canonical(sanitize(payload)).encode(), mtime=0),
                    "normalized.geojson": canonical(normalized["geojson"]).encode(),
                    "manifest.json": canonical(manifest).encode(),
                    "preview.svg": preview(normalized["geojson"], normalized["bounds"]).encode(),
                }
                for filename, content in files.items():
                    with (temporary / filename).open("xb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                os.replace(temporary, final)
                if os.name != "nt":
                    descriptor = os.open(final.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                db.execute(
                    "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        device,
                        set_id,
                        version,
                        normalized["fingerprint"],
                        name,
                        "",
                        created,
                        canonical(manifest),
                    ),
                )
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                # An unreferenced final directory is retained for crash recovery, never overwritten.
                raise
        self.store.event(
            "map_capture",
            "Immutable map snapshot created",
            {"id": key, "fingerprint": normalized["fingerprint"]},
        )
        return self.get(key) | {"unchanged": False}

    def metadata(self, key: str, name: str, notes: str):
        self.get(key)
        with self.store.connect() as db:
            db.execute("UPDATE snapshots SET name=?,notes=? WHERE id=?", (name, notes, key))
        return self.get(key)

    def delete(self, key: str):
        path = self.directory(key)
        with self.lock, self.store.connect() as db:
            db.execute("DELETE FROM snapshots WHERE id=?", (key,))
        shutil.rmtree(path)
        self.store.event("map_delete", "Snapshot explicitly deleted", {"id": key})

    def export(self, key: str) -> bytes:
        folder = self.directory(key)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("raw.json.gz", "normalized.geojson", "manifest.json", "preview.svg"):
                archive.write(folder / name, name)
            archive.writestr(
                "metadata.json", canonical({k: self.get(key)[k] for k in ("name", "notes")})
            )
        return output.getvalue()

    def import_zip(self, content: bytes):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if (
                len(infos) > 6
                or sum(x.file_size for x in infos) > MAX_BYTES
                or len({x.filename for x in infos}) != len(infos)
            ):
                raise ValueError("Archive exceeds import limits or contains duplicate entries")
            if any(
                x.filename
                not in {
                    "raw.json.gz",
                    "normalized.geojson",
                    "manifest.json",
                    "preview.svg",
                    "metadata.json",
                }
                for x in infos
            ):
                raise ValueError("Unsupported archive entry")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
                raise ValueError("Unsupported map archive schema")
            with gzip.GzipFile(fileobj=io.BytesIO(archive.read("raw.json.gz"))) as handle:
                raw_bytes = handle.read(MAX_BYTES + 1)
            if len(raw_bytes) > MAX_BYTES:
                raise ValueError("Raw map exceeds import limit")
            payload = json.loads(raw_bytes)
            normalized = normalize(payload)
            if normalized["fingerprint"] != manifest["fingerprint"] or canonical(
                normalized["geojson"]
            ) != canonical(json.loads(archive.read("normalized.geojson"))):
                raise ValueError("Archive integrity check failed")
            metadata = (
                json.loads(archive.read("metadata.json"))
                if "metadata.json" in archive.namelist()
                else manifest
            )
            origin_set = str(manifest.get("set_id", ""))
            payload.setdefault("source_metadata", {})["imported_from"] = {
                "set_id": origin_set,
                "snapshot_id": manifest.get("id"),
                "created": manifest.get("created"),
                "version": manifest.get("version"),
            }
            related = next(
                (
                    s
                    for s in self.list()
                    if s["device"] == str(manifest["device"])
                    and (
                        s["set_id"] == origin_set
                        or s["manifest"]
                        .get("source_metadata", {})
                        .get("imported_from", {})
                        .get("set_id")
                        == origin_set
                    )
                ),
                None,
            )
            result = self.capture(
                payload,
                str(manifest["device"]),
                str(metadata.get("name", "Imported map"))[:200],
                set_id=related["set_id"] if related else None,
                source="archive-import",
            )
            if not result["unchanged"]:
                self.metadata(result["id"], result["name"], str(metadata.get("notes", ""))[:4000])
            return result
