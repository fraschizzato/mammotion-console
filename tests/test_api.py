import json
from unittest.mock import AsyncMock

from tests.conftest import seed_ha


def test_health_spa_and_offline_archive(client, map_a, map_b):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/").status_code == 200
    a = client.post(
        "/api/maps/import", files={"file": ("a.json", json.dumps(map_a), "application/json")}
    )
    assert a.status_code == 200, a.text
    key = a.json()["id"]
    geo = client.get(f"/api/maps/{key}/geojson").content
    assert (
        client.post(
            "/api/maps/import", files={"file": ("b.json", json.dumps(map_b), "application/json")}
        ).status_code
        == 200
    )
    assert len(client.get("/api/maps").json()) == 2
    assert client.get(f"/api/maps/{key}/geojson").content == geo
    assert client.delete(f"/api/maps/{key}").status_code == 422
    assert client.delete(f"/api/maps/{key}?confirmed=true").status_code == 200
    assert client.get(f"/api/maps/{key}/geojson").status_code == 404


def test_control_confirmation_and_offline_guard(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.request = AsyncMock(return_value={"state": "idle"})
    assert client.post("/api/mower/dock", json={}).status_code == 422
    assert client.post("/api/mower/dock", json={"confirmed": True}).status_code == 200
    ha.request.assert_any_await(
        "POST", "/api/services/lawn_mower/dock", {"entity_id": "lawn_mower.luba"}
    )
    ha.connected = False
    ha.last_command = 0
    assert client.post("/api/mower/dock", json={"confirmed": True}).status_code == 503
    assert client.post("/api/movement/forward", json={}).status_code == 409


def test_jobs_scope_validation_and_presets(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.request = AsyncMock(return_value={"state": "idle"})
    fields = {"areas": ["switch.luba_orchard"], "speed": 0.4}
    # Identifiers are strings even for small hashes, preserving uint64 precision in browsers.
    assert client.get("/api/jobs").json()["zones"][0]["hash"] == "101"
    p = client.post("/api/jobs/presets", json={"name": "Morning", "fields": fields})
    assert p.status_code == 200, p.text
    assert (
        client.post("/api/jobs/start", json={"confirmed": True, "fields": fields}).status_code
        == 200
    )
    fields["areas"] = ["switch.other_mower_zone"]
    assert (
        client.post("/api/jobs/start", json={"confirmed": True, "fields": fields}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/jobs/start",
            json={"confirmed": True, "fields": {"areas": ["switch.luba_orchard"], "speed": 5}},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/schedules/delete", json={"confirmed": True, "target": "button.other"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/schedules/delete", json={"confirmed": True, "target": "button.luba_task"}
        ).status_code
        == 200
    )
    assert len(client.get("/api/schedules").json()["history"]) == 1


def test_csrf_and_secret_sanitizing(client):
    assert (
        client.post(
            "/api/maps/capture", headers={"Origin": "https://evil.example"}, json={}
        ).status_code
        == 403
    )
    client.headers.pop("X-Console-Request")
    assert client.post("/api/maps/capture", json={}).status_code == 403
    ha = client.app.state.ha
    seed_ha(ha)
    from pydantic import SecretStr

    ha.settings.ha_token = SecretStr("canary-never-browser")
    ha.states["camera.luba"]["attributes"] = {
        "access_token": "canary-never-browser",
        "friendly_name": "canary-never-browser",
    }
    for path in ["/api/state", "/api/settings", "/api/diagnostics"]:
        assert "canary-never-browser" not in client.get(path).text


def test_live_websocket_initial_state(client):
    seed_ha(client.app.state.ha)
    with client.websocket_connect("/api/live") as ws:
        state = ws.receive_json()
        assert state["metrics"][0]["value"] == "87"
        assert state["device"]["name"] == "LUBA Garden"
