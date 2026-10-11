import copy
import gzip
import io
import json
import math
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.maps import MapArchive, normalize
from backend.providers import (
    CacheSource,
    local_map_geojson,
    map_evidence,
    merge_missing_map_features,
)
from backend.storage import Store
from tests.conftest import FIXTURES


def test_cloud_map_switch_preserves_every_byte_and_version(tmp_path, map_a, map_b):
    archive = MapArchive(Store(tmp_path))
    a = archive.capture(map_a, "mower", "A")
    original = {p.name: p.read_bytes() for p in archive.directory(a["id"]).iterdir()}
    b = archive.capture(map_b, "mower", "B")
    assert len(archive.list()) == 2
    assert a["set_id"] != b["set_id"]
    assert archive.capture(map_a, "mower", "A")["id"] == a["id"]
    assert {p.name: p.read_bytes() for p in archive.directory(a["id"]).iterdir()} == original
    changed = copy.deepcopy(map_a)
    changed["geojson"]["features"][0]["geometry"]["coordinates"][0][1][0] += 0.0001
    v2 = archive.capture(changed, "mower", "A v2", set_id=a["set_id"])
    assert v2["version"] == 2 and v2["fingerprint"] != a["fingerprint"]
    assert len(archive.list()) == 3
    assert {p.name: p.read_bytes() for p in archive.directory(a["id"]).iterdir()} == original
    reopened = MapArchive(Store(tmp_path))
    assert len(reopened.list()) == 3
    reopened.metadata(a["id"], "Renamed", "Notes")
    assert {p.name: p.read_bytes() for p in reopened.directory(a["id"]).iterdir()} == original


def test_canonical_ignores_live_pose_order_ring_direction(tmp_path, map_a):
    expected = normalize(map_a)["fingerprint"]
    map_a["timestamp"] = "tomorrow"
    map_a["position"] = {"latitude": 0, "longitude": 0}
    map_a["geojson"]["features"].reverse()
    for f in map_a["geojson"]["features"]:
        if f["geometry"]["type"] == "Polygon":
            f["geometry"]["coordinates"][0].reverse()
        f["properties"]["progress"] = 30
    assert normalize(map_a)["fingerprint"] == expected


def test_incomplete_and_progress_rejected(tmp_path, map_a):
    archive = MapArchive(Store(tmp_path))
    with pytest.raises(ValueError, match="static work zones"):
        archive.capture(json.loads((FIXTURES / "progress-only.json").read_text()), "m", "progress")
    map_a["expected_zone_ids"].append("missing")
    assert normalize(map_a)["completeness"] == "partial"
    with pytest.raises(ValueError, match="partial"):
        archive.capture(map_a, "m", "partial")
    assert archive.list() == []
    del map_a["expected_zone_ids"]
    assert normalize(map_a)["completeness"] == "uncertain"


def test_export_import_integrity_and_secret_removal(tmp_path, map_a):
    one, two = MapArchive(Store(tmp_path / "one")), MapArchive(Store(tmp_path / "two"))
    map_a["access_token"] = "secret-canary"
    a = one.capture(map_a, "m", "Original")
    one.metadata(a["id"], "Updated name", "Notes survive export")
    blob = one.export(a["id"])
    imported = two.import_zip(blob)
    assert imported["fingerprint"] == a["fingerprint"]
    assert two.get(imported["id"])["notes"] == "Notes survive export"
    assert imported["name"] == "Updated name"
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert b"secret-canary" not in gzip.decompress(z.read("raw.json.gz"))
    assert two.import_zip(blob)["unchanged"]
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../../attack", "evil")
    with pytest.raises(ValueError, match="Unsupported archive"):
        two.import_zip(bad.getvalue())


def test_concurrent_capture_deduplicates(tmp_path, map_a):
    archive = MapArchive(Store(tmp_path))
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda _: archive.capture(map_a, "m", "A")["id"], range(8)))
    assert len(set(ids)) == 1


def test_frame_completeness_and_static_geometry(map_a):
    raw = {
        "area": {"101": {"total_frame": 2, "data": [{"current_frame": 1}]}},
        "area_name": [{"hash": 102, "name": "missing"}],
    }
    payload = map_evidence(raw, map_a["geojson"], "test")
    assert normalize(payload)["completeness"] == "partial"
    raw["area"]["101"]["data"].append({"current_frame": 1})
    assert not map_evidence(raw, map_a["geojson"], "test")["frames_complete"]
    assert {f["properties"]["kind"] for f in normalize(map_a)["geojson"]["features"]} >= {
        "zone",
        "rtk",
        "dock",
        "obstacle",
        "path",
    }


def test_cache_read_only_and_xy_transform(tmp_path, map_a):
    root = tmp_path / "ha"
    (root / ".storage").mkdir(parents=True)
    raw = {
        "area": {
            "101": {
                "total_frame": 1,
                "data": [
                    {
                        "current_frame": 1,
                        "data_couple": [
                            {"x": 0, "y": 0},
                            {"x": 10, "y": 0},
                            {"x": 10, "y": 10},
                            {"x": 0, "y": 0},
                        ],
                    }
                ],
            }
        },
        "area_name": [{"hash": 101, "name": "Lawn"}],
    }
    device = {"map": raw, "location": {"RTK": {"latitude": 45, "longitude": 9, "yaw": 0}}}
    path = root / ".storage/mammotion.fixture"
    path.write_text(
        json.dumps(
            {"version": 1, "minor_version": 2, "data": {"devices": {"Luba-fixture": device}}}
        )
    )
    original = path.read_bytes()
    payload = CacheSource(root).fetch("Luba-fixture")
    assert normalize(payload)["completeness"] == "complete"
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="origin"):
        local_map_geojson(raw, {})


def test_path_traversal_and_invalid_geo(tmp_path, map_a):
    archive = MapArchive(Store(tmp_path))
    with pytest.raises(ValueError):
        archive.directory("../../etc")
    map_a["geojson"]["features"][0]["geometry"]["coordinates"][0][0] = [999, 999]
    with pytest.raises(ValueError):
        normalize(map_a)


def test_local_map_geojson_converts_pymammotion_radians_and_local_dock():
    # Synthetic location, unrelated to any real installation.
    origin_lat = 45.0
    origin_lon = 10.0

    raw = {
        "area": {
            "101": {
                "total_frame": 1,
                "data": [
                    {
                        "current_frame": 1,
                        "data_couple": [
                            {"x": 0, "y": 0},
                            {"x": 10, "y": 0},
                            {"x": 10, "y": 10},
                            {"x": 0, "y": 0},
                        ],
                    }
                ],
            }
        },
        "area_name": [{"hash": 101, "name": "Lawn"}],
    }

    location = {
        "RTK": {
            "latitude": math.radians(origin_lat),
            "longitude": math.radians(origin_lon),
            "yaw": 0.9,
        },
        "device": {
            "latitude": origin_lat + 0.0002,
            "longitude": origin_lon - 0.0001,
        },
        "dock": {
            "latitude": -0.074,
            "longitude": -0.076,
        },
    }

    geojson = local_map_geojson(raw, location)

    by_kind = {
        (
            feature["properties"].get("kind")
            or feature["properties"].get("type_name")
        ): feature
        for feature in geojson["features"]
    }

    assert by_kind["rtk"]["geometry"]["coordinates"] == pytest.approx(
        [origin_lon, origin_lat]
    )

    dock_lon, dock_lat = by_kind["dock"]["geometry"]["coordinates"]

    # The local dock must be converted near the RTK origin,
    # rather than treated as a geographic coordinate.
    assert abs(dock_lon - origin_lon) < 0.001
    assert abs(dock_lat - origin_lat) < 0.001
    assert (dock_lon, dock_lat) != (origin_lon, origin_lat)

    zone_coordinates = by_kind["area"]["geometry"]["coordinates"][0]

    assert all(
        abs(lon - origin_lon) < 0.001
        for lon, _ in zone_coordinates
    )
    assert all(
        abs(lat - origin_lat) < 0.001
        for _, lat in zone_coordinates
    )


def test_merge_missing_map_features_preserves_sdk_geometry():
    generated = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"type_name": "area", "hash": "101"},
                "geometry": {"type": "Point", "coordinates": [12.09, 43.21]},
            }
        ],
    }
    rebuilt = {
        "type": "FeatureCollection",
        "features": [
            {**generated["features"][0], "geometry": {"type": "Point", "coordinates": [1, 1]}},
            {
                "type": "Feature",
                "properties": {"type_name": "path", "hash": "202"},
                "geometry": {"type": "Point", "coordinates": [0.091, 0.211]},
            },
        ],
    }

    merged = merge_missing_map_features(generated, rebuilt)

    assert merged["features"][0]["geometry"]["coordinates"] == [12.09, 43.21]
    assert merged["features"][1]["properties"]["hash"] == "202"
