from unittest.mock import AsyncMock

from backend.jobs import zones_match_snapshot
from tests.conftest import seed_ha


def test_snapshot_zone_matching_is_one_to_one_and_fail_closed():
    archived = [
        {"source_id": "101", "geometry_fingerprint": "geo-a", "name": "Orchard"},
        {"source_id": "102", "geometry_fingerprint": "geo-b", "name": "Home lawn"},
    ]
    assert zones_match_snapshot(
        [{"hash": "101", "name": "changed"}, {"hash": "102", "name": "changed"}],
        archived,
    )
    assert zones_match_snapshot(
        [
            {"hash": "901", "geometry_fingerprint": "geo-a", "name": "Zone 901"},
            {"hash": "902", "geometry_fingerprint": "geo-b", "name": "Zone 902"},
        ],
        archived,
    )
    assert zones_match_snapshot(
        [{"hash": "901", "name": "Orchard"}, {"hash": "902", "name": "Home lawn"}],
        archived,
    )
    assert zones_match_snapshot([], archived) is None
    assert zones_match_snapshot([{"hash": "101"}], archived) is False

    ambiguous = [
        {"source_id": "201", "name": "Lawn"},
        {"source_id": "202", "name": "Lawn"},
    ]
    assert zones_match_snapshot([{"hash": "999", "name": "Lawn"}] * 2, ambiguous) is False
    assert zones_match_snapshot([{"hash": None, "name": "Zone 1"}], [{"name": "Zone 1"}]) is False


def test_overlay_is_returned_only_for_the_matching_archived_map(client, map_a):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.services["mammotion"]["get_mow_path_geojson"] = {}
    path = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[9.181, 45.461], [9.1811, 45.4611]],
                },
            }
        ],
    }
    ha.request = AsyncMock(return_value={"service_response": path})

    matching = {
        "name": "Current map",
        "device": "Luba-fixture",
        "expected_zone_ids": ["101"],
        "geojson": {
            "type": "FeatureCollection",
            "features": [map_a["geojson"]["features"][0]],
        },
    }
    snapshot = client.app.state.archive.capture(matching, "Luba-fixture", "Current")
    response = client.get(f"/api/overlay?snapshot_id={snapshot['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"]["snapshot_matches"] is True
    assert body["status"]["route_current"] is None
    assert body["path"] == path
    assert ha.request.await_count == 1

    ha.request.reset_mock()
    response = client.get(f"/api/overlay?snapshot_id={snapshot['id']}&include_route=false")
    assert response.status_code == 200
    assert response.json()["status"]["snapshot_matches"] is True
    assert "path" not in response.json()
    ha.request.assert_not_awaited()

    different = client.app.state.archive.capture(map_a, "Luba-fixture", "Different")
    ha.request.reset_mock()
    response = client.get(f"/api/overlay?snapshot_id={different['id']}")
    assert response.status_code == 200
    assert response.json()["status"]["snapshot_matches"] is False
    assert "path" not in response.json()
    ha.request.assert_not_awaited()


def test_overlay_rejects_unknown_snapshot(client):
    response = client.get("/api/overlay?snapshot_id=00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
