from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.ha import HAError
from backend.jobs import Jobs
from backend.storage import Store
from tests.conftest import seed_ha


def test_history_preserves_settings_and_revalidates_current_zones(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.request = AsyncMock(return_value={"state": "idle"})
    fields = {"areas": ["switch.luba_orchard"], "speed": 0.4, "modify": False}
    response = client.post("/api/jobs/start", json={"confirmed": True, "fields": fields})
    assert response.status_code == 200
    key = response.json()["history_id"]
    history = client.get("/api/jobs").json()["history"]
    assert history[0]["zones"][0]["settings"]["speed"] == 0.4
    assert history[0]["fields"] == fields
    assert Store(ha.store.root).documents("job_history")[0]["id"] == key
    reused = client.get(f"/api/jobs/history/{key}/fields").json()["fields"]
    assert reused == {"areas": fields["areas"], "speed": 0.4}
    ha.states["switch.luba_orchard"]["attributes"]["hash"] = 999
    # Mammotion may rotate an area hash while the server-resolved human name
    # still identifies one current zone. Reuse targets its current entity.
    reused = client.get(f"/api/jobs/history/{key}/fields").json()["fields"]
    assert reused == {"areas": fields["areas"], "speed": 0.4}
    ha.states["switch.luba_orchard"]["attributes"]["friendly_name"] = "Zone 999"
    assert client.get(f"/api/jobs/history/{key}/fields").status_code == 422
    assert client.get("/api/jobs").json()["history"][0]["fields"] == fields
    ha.devices[0]["name"] = "Different-mower"
    assert client.get(f"/api/jobs/history/{key}/fields").status_code == 404


def test_history_name_fallback_rejects_ambiguous_current_zones(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.request = AsyncMock(return_value={"state": "idle"})
    response = client.post(
        "/api/jobs/start",
        json={
            "confirmed": True,
            "fields": {"areas": ["switch.luba_orchard"], "speed": 0.4},
        },
    )
    key = response.json()["history_id"]
    ha.states["switch.luba_orchard"]["attributes"]["hash"] = 999
    ha.states["switch.luba_duplicate"] = {
        "entity_id": "switch.luba_duplicate",
        "state": "on",
        "attributes": {"friendly_name": "Orchard", "hash": 1000},
    }
    ha.registry.append(
        {
            "entity_id": "switch.luba_duplicate",
            "device_id": "dev-1",
            "platform": "mammotion",
            "unique_id": "switch.luba_duplicate",
            "translation_key": "duplicate",
        }
    )
    assert client.get(f"/api/jobs/history/{key}/fields").status_code == 422


def test_uncertain_delivery_is_recorded_without_claiming_job_started(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.request = AsyncMock(return_value={"state": "idle"})
    ha.call = AsyncMock(side_effect=HAError("Connection lost"))
    response = client.post(
        "/api/jobs/start",
        json={
            "confirmed": True,
            "fields": {"areas": ["switch.luba_orchard"]},
        },
    )
    assert response.status_code == 503
    assert client.get("/api/jobs").json()["history"][0]["status"] == "delivery_unknown"


def test_resume_requires_current_paused_job_and_confirmation(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.request = AsyncMock(return_value={"state": "idle"})
    ha.control = AsyncMock(return_value={"accepted": True})
    assert client.post("/api/jobs/resume", json={}).status_code == 422
    assert client.post("/api/jobs/resume", json={"confirmed": True}).status_code == 422
    ha.control.assert_not_awaited()
    ha.request.return_value = {"state": "paused"}
    assert client.post("/api/jobs/resume", json={"confirmed": True}).status_code == 200
    ha.control.assert_awaited_once_with("resume")


async def test_observed_history_does_not_invent_completed_status(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.request = AsyncMock(return_value={"state": "idle"})
    ha.call = AsyncMock()
    jobs = Jobs(ha)
    await jobs.start({"areas": ["switch.luba_orchard"], "speed": 0.5})
    jobs.observe()
    assert jobs.history()[0]["status"] == "requested"
    ha.states["lawn_mower.luba"]["state"] = "mowing"
    jobs.observe()
    ha.states["lawn_mower.luba"]["state"] = "paused"
    jobs.observe()
    assert jobs.history()[0]["status"] == "paused"
    ha.states["lawn_mower.luba"]["state"] = "docked"
    jobs.observe()
    assert jobs.history()[0]["status"] == "recharging"
    assert jobs.active_history
    restarted = Jobs(ha)
    restarted.observe()
    assert restarted.active_history == jobs.active_history
    ha.states["lawn_mower.luba"]["state"] = "idle"
    restarted.observe()
    assert restarted.history()[0]["status"] == "ended_unknown"
    assert not restarted.active_history


def test_job_route_survives_restart_and_keeps_growing_trace(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.states["lawn_mower.luba"]["state"] = "mowing"
    jobs = Jobs(ha)
    jobs.observe()
    key = jobs.active_history
    assert key
    first = {
        "traveled": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "source": "mammotion_mqtt_position_trace",
                        "width_m": 0.25,
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[12.0, 43.0], [12.00001, 43.0]],
                    },
                }
            ],
        },
        "mowed": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"area_m2": 0.2, "width_m": 0.25},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[12.0, 43.0], [12.0, 43.0], [12.0, 43.0]]],
                    },
                }
            ],
        },
    }
    jobs.capture_overlay(first)
    assert jobs.history()[0]["route"]["traveled_points"] == 2
    assert (ha.store.root / "jobs" / f"{key}.geojson.json.gz").is_file()

    restarted = Jobs(ha)
    restarted.observe()
    assert restarted.active_history == key
    longer = first | {
        "traveled": {
            "type": "FeatureCollection",
            "features": [
                first["traveled"]["features"][0]
                | {
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [12.0, 43.0],
                            [12.00001, 43.0],
                            [12.00002, 43.0],
                        ],
                    }
                }
            ],
        }
    }
    restarted.capture_overlay(longer)
    saved = restarted.route(key)
    assert len(saved["traveled"]["features"][0]["geometry"]["coordinates"]) == 3
    assert saved["mowed"]["features"]


def test_job_history_overlay_endpoint_is_device_scoped(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.states["lawn_mower.luba"]["state"] = "paused"
    jobs = client.app.state.jobs
    jobs.observe()
    key = jobs.active_history
    assert key
    jobs.capture_overlay(
        {
            "path": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[12.0, 43.0], [12.1, 43.1]],
                        },
                    }
                ],
            }
        }
    )
    response = client.get(f"/api/jobs/history/{key}/overlay")
    assert response.status_code == 200
    assert response.json()["status"]["historical"] is True
    assert len(response.json()["path"]["features"]) == 1
    ha.devices[0]["name"] = "Different-mower"
    assert client.get(f"/api/jobs/history/{key}/overlay").status_code == 404


def test_live_overlay_falls_back_to_persisted_active_job(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.states["lawn_mower.luba"]["state"] = "paused"
    jobs = client.app.state.jobs
    jobs.observe()
    jobs.capture_overlay(
        {
            "traveled": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"source": "mammotion_mqtt_position_trace"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[12.0, 43.0], [12.00001, 43.0]],
                        },
                    }
                ],
            }
        }
    )
    response = client.get("/api/overlay")
    assert response.status_code == 200
    assert response.json()["status"]["persisted"] is True
    assert len(response.json()["traveled"]["features"]) == 1


def test_secondary_cloud_job_translates_zone_hash_to_ha_history(client):
    ha = client.app.state.ha
    seed_ha(ha)
    source = SimpleNamespace(
        telemetry=lambda: {"online": True, "state": "paused", "position": None},
        current_job=lambda: {
            "job_id": "cloud-42",
            "fields": {"areas": ["switch.zone_101"], "speed": 0.45},
        },
        entities=lambda: [
            {
                "entity_id": "switch.zone_101",
                "state": "on",
                "attributes": {"friendly_name": "Zone 101", "hash": "101"},
            }
        ],
        zone_geometry_details=lambda: {},
    )
    jobs = Jobs(ha)
    jobs.observe(source)
    record = jobs.history()[0]
    assert record["device_job_id"] == "cloud-42"
    assert record["reported_fields"]["areas"] == ["switch.luba_orchard"]
    assert record["zones"][0]["entity_id"] == "switch.luba_orchard"


def test_background_route_archive_does_not_require_an_open_browser(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.states["lawn_mower.luba"]["state"] = "paused"
    ha.services["mammotion"]["get_dynamics_line_geojson"] = {}
    trace = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"source": "home_assistant_dynamics"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[12.0, 43.0], [12.00001, 43.0]],
                },
            }
        ],
    }
    ha.call = AsyncMock(return_value={"service_response": trace})

    summary = client.portal.call(client.app.state.archive_active_job_route)
    assert summary["traveled_points"] == 2
    history = client.get("/api/jobs").json()["history"]
    assert history[0]["route"]["available"] is True
    overlay = client.get(f"/api/jobs/history/{history[0]['id']}/overlay").json()
    assert overlay["traveled"] == trace
    assert client.get("/api/health").json()["features"]["persistent_job_routes"] == 1
