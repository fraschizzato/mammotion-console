from copy import deepcopy
from unittest.mock import AsyncMock

from backend.jobs import Jobs
from backend.storage import Store
from tests.conftest import seed_ha


def test_uint64_zone_ids_remain_exact_through_api_and_job_validation(client, map_a):
    ha = client.app.state.ha
    seed_ha(ha)
    zone_id = "2163392998022310366"
    ha.states["switch.luba_orchard"]["attributes"]["hash"] = int(zone_id)
    map_a["expected_zone_ids"][0] = zone_id
    map_a["geojson"]["features"][0]["properties"]["hash"] = zone_id
    snapshot = client.app.state.archive.capture(map_a, "Luba-fixture", "Uint64 map")
    response = client.get("/api/jobs")
    assert response.json()["zones"][0]["hash"] == zone_id
    assert f'"hash":"{zone_id}"' in response.text
    ha.request = AsyncMock(return_value={"state": "docked"})
    ha.call = AsyncMock()
    body = {
        "map_id": snapshot["id"],
        "fields": {"areas": ["switch.luba_orchard"], "speed": 0.3},
        "confirmed": True,
    }
    assert client.post("/api/jobs/start", json=body).status_code == 200
    ha.call.assert_awaited_once_with(
        "mammotion", "start_mow", body["fields"] | {"entity_id": "lawn_mower.luba"}
    )


def test_profiles_are_independent_persistent_and_local_only(client, map_a, map_b):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.call = AsyncMock()
    archive = client.app.state.archive
    a = archive.capture(map_a, "Luba-fixture", "A")
    b = archive.capture(map_b, "Luba-fixture", "B")
    original = client.get(f"/api/maps/{a['id']}/geojson").content
    for zone, speed in (("101", 0.3), ("102", 0.7)):
        response = client.put(
            "/api/jobs/zone-profile",
            json={"map_id": a["id"], "zone_hash": zone, "fields": {"speed": speed}},
        )
        assert response.status_code == 200, response.text
    # Recreating the storage facade preserves both independently saved profiles.
    saved = Store(client.app.state.store.root).documents("zone_profile")
    assert {p["zone_hash"]: p["fields"]["speed"] for p in saved} == {"101": 0.3, "102": 0.7}
    assert len(client.get("/api/jobs").json()["zone_profiles"]) == 2
    update = {"map_id": a["id"], "zone_hash": "101", "fields": {"speed": 0.4}}
    assert client.put("/api/jobs/zone-profile", json=update).status_code == 200
    saved = client.get("/api/jobs").json()["zone_profiles"]
    assert len(saved) == 2
    assert next(p for p in saved if p["zone_hash"] == "102")["fields"]["speed"] == 0.7
    assert (
        client.put("/api/jobs/zone-profile", json=update | {"map_id": b["id"]}).status_code == 422
    )
    assert client.put("/api/jobs/zone-profile", json=update | {"zone_hash": 101}).status_code == 422
    for fields in (
        {"speed": 5},
        {"areas": ["switch.luba_orchard"]},
        {"modify": True},
        {"start_progress": 20},
    ):
        assert (
            client.put("/api/jobs/zone-profile", json=update | {"fields": fields}).status_code
            == 422
        )
    ha.call.assert_not_awaited()
    assert client.get(f"/api/maps/{a['id']}/geojson").content == original


def test_zone_hash_drift_uses_archived_geometry_for_name_profile_and_launch(client, map_a):
    ha = client.app.state.ha
    seed_ha(ha)
    snapshot = client.app.state.archive.capture(map_a, "Luba-fixture", "A")
    archived_zone = next(
        zone for zone in snapshot["manifest"]["zones"] if zone["source_id"] == "101"
    )
    current_hash = "9007199254741999"
    ha.states["switch.luba_orchard"]["attributes"] |= {
        "hash": int(current_hash),
        "friendly_name": f"Zone {current_hash}",
    }
    ha.zone_geometry_details = lambda: {
        current_hash: {
            "geometry_fingerprint": archived_zone["geometry_fingerprint"],
            "area_m2": 789.4,
            "mapped_name": None,
        }
    }

    zone = client.get("/api/jobs").json()["zones"][0]
    assert zone == {
        "entity_id": "switch.luba_orchard",
        "name": "Orchard",
        "hash": current_hash,
        "geometry_fingerprint": archived_zone["geometry_fingerprint"],
        "area_m2": 789.4,
    }
    profile = client.put(
        "/api/jobs/zone-profile",
        json={
            "map_id": snapshot["id"],
            "zone_hash": current_hash,
            "zone_fingerprint": archived_zone["geometry_fingerprint"],
            "fields": {"speed": 0.3},
        },
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["map_zone_hash"] == "101"
    assert profile.json()["zone_name"] == "Orchard"

    ha.request = AsyncMock(return_value={"state": "docked"})
    ha.call = AsyncMock()
    launch = client.post(
        "/api/jobs/zone-sequence",
        json={
            "map_id": snapshot["id"],
            "zone_hashes": [current_hash],
            "confirmed": True,
        },
    )
    assert launch.status_code == 200, launch.text
    assert ha.call.await_args.args[2]["areas"] == ["switch.luba_orchard"]


def test_zone_area_mapping_recovers_name_when_hash_and_geometry_change(client, map_a):
    ha = client.app.state.ha
    seed_ha(ha)
    snapshot = client.app.state.archive.capture(map_a, "Luba-fixture", "A")
    current_hash = "9007199254742001"
    ha.states["switch.luba_orchard"]["attributes"] |= {
        "hash": int(current_hash),
        "friendly_name": f"Zone {current_hash}",
    }
    ha.zone_geometry_details = lambda: {
        current_hash: {
            "geometry_fingerprint": "f" * 64,
            "area_m2": 789.6,
            "mapped_name": "Orchard",
        }
    }

    zone = client.get("/api/jobs").json()["zones"][0]
    assert zone["name"] == "Orchard"
    assert zone["area_m2"] == 789.6
    profile = client.put(
        "/api/jobs/zone-profile",
        json={
            "map_id": snapshot["id"],
            "zone_hash": current_hash,
            "zone_fingerprint": "f" * 64,
            "fields": {"speed": 0.3},
        },
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["zone_name"] == "Orchard"
    assert profile.json()["map_zone_hash"] == "101"


def test_name_fallback_rejects_ambiguous_archived_zones(client, map_a):
    ha = client.app.state.ha
    seed_ha(ha)
    ambiguous = deepcopy(map_a)
    for feature in ambiguous["geojson"]["features"]:
        if feature["properties"].get("type_name") == "area":
            feature["properties"]["Name"] = "Orchard"
    snapshot = client.app.state.archive.capture(ambiguous, "Luba-fixture", "Ambiguous")
    current_hash = "9007199254742002"
    ha.states["switch.luba_orchard"]["attributes"] |= {
        "hash": int(current_hash),
        "friendly_name": f"Zone {current_hash}",
    }
    ha.zone_geometry_details = lambda: {
        current_hash: {
            "geometry_fingerprint": "e" * 64,
            "area_m2": 789,
            "mapped_name": "Orchard",
        }
    }

    response = client.put(
        "/api/jobs/zone-profile",
        json={
            "map_id": snapshot["id"],
            "zone_hash": current_hash,
            "zone_fingerprint": "e" * 64,
            "fields": {"speed": 0.3},
        },
    )
    assert response.status_code == 422
    assert "Choose a zone belonging" in response.text


def test_confirmed_zone_sequence_runs_saved_profiles_in_order(client, map_a):
    ha = client.app.state.ha
    seed_ha(ha)
    meadow = {
        "entity_id": "switch.luba_meadow",
        "state": "on",
        "attributes": {"friendly_name": "Meadow", "hash": 102},
    }
    ha.states[meadow["entity_id"]] = meadow
    ha.registry.append(
        {
            "entity_id": meadow["entity_id"],
            "device_id": "dev-1",
            "platform": "mammotion",
            "unique_id": meadow["entity_id"],
            "translation_key": "meadow",
        }
    )
    ha.call = AsyncMock()
    ha.request = AsyncMock(
        side_effect=lambda *_args, **_kwargs: ha.states["lawn_mower.luba"].copy()
    )
    snapshot = client.app.state.archive.capture(map_a, "Luba-fixture", "A")
    for zone_hash, speed in (("101", 0.3), ("102", 0.7)):
        response = client.put(
            "/api/jobs/zone-profile",
            json={"map_id": snapshot["id"], "zone_hash": zone_hash, "fields": {"speed": speed}},
        )
        assert response.status_code == 200, response.text

    body = {
        "map_id": snapshot["id"],
        "zone_hashes": ["101", "102"],
        "confirmed": True,
    }
    assert (
        client.post("/api/jobs/zone-sequence", json=body | {"confirmed": False}).status_code == 422
    )
    response = client.post("/api/jobs/zone-sequence", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["zone_count"] == 2
    assert ha.call.await_args_list[0].args[2]["areas"] == ["switch.luba_orchard"]
    assert ha.call.await_args_list[0].args[2]["speed"] == 0.3
    assert (
        client.post(
            "/api/jobs/start",
            json={"confirmed": True, "fields": {"areas": ["switch.luba_orchard"]}},
        ).status_code
        == 422
    )

    jobs = client.app.state.jobs
    ha.states["lawn_mower.luba"]["state"] = "mowing"
    client.portal.call(jobs.advance_sequence)
    ha.states["lawn_mower.luba"]["state"] = "docked"
    client.portal.call(jobs.advance_sequence)
    assert len(ha.call.await_args_list) == 2
    assert ha.call.await_args_list[1].args[2]["areas"] == ["switch.luba_meadow"]
    assert ha.call.await_args_list[1].args[2]["speed"] == 0.7
    ha.states["lawn_mower.luba"]["state"] = "mowing"
    client.portal.call(jobs.advance_sequence)
    ha.states["lawn_mower.luba"]["state"] = "docked"
    client.portal.call(jobs.advance_sequence)

    sequence = client.get("/api/jobs").json()["zone_sequences"][0]
    assert sequence["status"] == "completed"
    assert [item["zone_hash"] for item in sequence["items"]] == ["101", "102"]
    assert len(client.get("/api/jobs").json()["history"]) == 2


def test_zone_sequence_requires_profiles_and_cancel_stops_future_launch(client, map_a):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.request = AsyncMock(return_value={"state": "docked"})
    ha.call = AsyncMock()
    snapshot = client.app.state.archive.capture(map_a, "Luba-fixture", "A")
    body = {
        "map_id": snapshot["id"],
        "zone_hashes": ["101"],
        "confirmed": True,
    }
    assert client.post("/api/jobs/zone-sequence", json=body).status_code == 422
    assert (
        client.put(
            "/api/jobs/zone-profile",
            json={"map_id": snapshot["id"], "zone_hash": "101", "fields": {"speed": 0.4}},
        ).status_code
        == 200
    )
    assert client.post("/api/jobs/zone-sequence", json=body).status_code == 200
    assert client.post("/api/jobs/zone-sequence/cancel", json={}).status_code == 422
    assert (
        client.post("/api/jobs/zone-sequence/cancel", json={"confirmed": True}).json()["cancelled"]
        is True
    )
    assert client.get("/api/jobs").json()["zone_sequences"][0]["status"] == "cancelled"


def test_restart_never_retries_a_sequence_with_uncertain_delivery(client):
    ha = client.app.state.ha
    seed_ha(ha)
    record = ha.store.put(
        "job_sequence",
        {
            "device": "Luba-fixture",
            "status": "starting",
            "current_index": 1,
            "items": [{"zone_hash": "101"}, {"zone_hash": "102"}],
            "transitions": [],
        },
    )
    Jobs(ha)
    recovered = next(
        item for item in ha.store.documents("job_sequence") if item["id"] == record["id"]
    )
    assert recovered["status"] == "needs_review"
    assert recovered["transitions"][-1]["reason"] == "restart_during_delivery"
