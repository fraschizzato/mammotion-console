import base64
from unittest.mock import AsyncMock, Mock

import pytest

from backend.ha import HAError
from backend.ha_camera import agora_payload
from tests.conftest import seed_ha


def selects(ha):
    seed_ha(ha)
    ha.services["select"] = {"select_option": {}}
    for suffix, key, options in (
        ("modalita_di_svolta", "turning_mode", ["multipoint", "zero_turn"]),
        ("rilevamento_ostacoli", "bypass_mode", ["direct_touch", "no_touch", "sensitive"]),
    ):
        entity = "select.luba_va_fixture0001_" + suffix
        ha.states[entity] = {
            "entity_id": entity,
            "state": options[0],
            "attributes": {"options": options},
        }
        ha.registry.append(
            {
                "entity_id": entity,
                "device_id": "dev-1",
                "platform": "mammotion",
                "translation_key": key,
            }
        )


def auto_direction_switch(ha):
    ha.services["switch"] = {"turn_on": {}, "turn_off": {}}
    entity = "switch.luba_va_fixture0001_inversione_automatica_della_direzione_di_taglio"
    ha.states[entity] = {"entity_id": entity, "state": "off", "attributes": {}}
    ha.registry.append(
        {
            "entity_id": entity,
            "device_id": "dev-1",
            "platform": "mammotion",
            "translation_key": "auto_change_direction",
        }
    )


def test_selects_preserved_in_preset_and_applied_before_start(client):
    ha = client.app.state.ha
    selects(ha)
    schema = client.get("/api/jobs").json()["capability"]["fields"]
    assert schema["ha_turning_mode"]["selector"]["select"]["options"] == ["multipoint", "zero_turn"]
    fields = {
        "areas": ["switch.luba_orchard"],
        "ha_turning_mode": "multipoint",
        "ha_bypass_mode": "no_touch",
    }
    saved = client.post("/api/jobs/presets", json={"name": "With HA selects", "fields": fields})
    assert saved.status_code == 200
    assert saved.json()["fields"] == fields
    ha.request = AsyncMock(
        side_effect=lambda method, path: ha.states[path.removeprefix("/api/states/")]
    )
    ha.call = AsyncMock()
    response = client.post("/api/jobs/start", json={"confirmed": True, "fields": fields})
    assert response.status_code == 200, response.text
    calls = ha.call.await_args_list
    assert [c.args[:2] for c in calls] == [
        ("select", "select_option"),
        ("select", "select_option"),
        ("mammotion", "start_mow"),
    ]
    assert calls[-1].args[2]["ultra_wave"] == 10
    assert "ha_turning_mode" not in calls[-1].args[2]
    assert client.get("/api/jobs").json()["history"][0]["fields"] == fields
    record = client.get("/api/jobs").json()["history"][0]
    record["reported_fields"] = {"ultra_wave": "10", "speed": 0.4}
    client.app.state.store.put("job_history", record, record["id"])
    restored = client.get(f"/api/jobs/history/{record['id']}/fields").json()["fields"]
    assert restored["ha_turning_mode"] == "multipoint"
    assert restored["ha_bypass_mode"] == "no_touch"
    assert "ultra_wave" not in restored


def test_ha_auto_direction_switch_is_applied_before_start(client):
    ha = client.app.state.ha
    seed_ha(ha)
    auto_direction_switch(ha)
    schema = client.get("/api/jobs").json()["capability"]["fields"]
    assert schema["ha_auto_change_direction"]["selector"] == {"boolean": {}}
    fields = {
        "areas": ["switch.luba_orchard"],
        "ha_auto_change_direction": True,
    }
    ha.request = AsyncMock(
        side_effect=lambda method, path: ha.states[path.removeprefix("/api/states/")]
    )
    ha.call = AsyncMock()
    response = client.post("/api/jobs/start", json={"confirmed": True, "fields": fields})
    assert response.status_code == 200, response.text
    calls = ha.call.await_args_list
    assert [call.args[:2] for call in calls] == [
        ("switch", "turn_on"),
        ("mammotion", "start_mow"),
    ]
    assert "ha_auto_change_direction" not in calls[-1].args[2]


def test_disappearing_ha_auto_direction_switch_never_starts_job(client):
    ha = client.app.state.ha
    seed_ha(ha)
    auto_direction_switch(ha)
    discovered = ha.job_switches()
    ha.job_switches = Mock(side_effect=[discovered, {}])
    ha.request = AsyncMock(
        side_effect=lambda method, path: ha.states[path.removeprefix("/api/states/")]
    )
    ha.call = AsyncMock()
    response = client.post(
        "/api/jobs/start",
        json={
            "confirmed": True,
            "fields": {
                "areas": ["switch.luba_orchard"],
                "ha_auto_change_direction": True,
            },
        },
    )
    assert response.status_code == 503
    assert "became unavailable" in response.text
    ha.call.assert_not_awaited()


def test_unavailable_select_and_failed_write_do_not_start(client):
    ha = client.app.state.ha
    selects(ha)
    fields = {"areas": ["switch.luba_orchard"], "ha_turning_mode": "multipoint"}
    ha.request = AsyncMock(
        side_effect=lambda method, path: ha.states[path.removeprefix("/api/states/")]
    )
    ha.call = AsyncMock(side_effect=HAError("Select failed"))
    assert (
        client.post("/api/jobs/start", json={"confirmed": True, "fields": fields}).status_code
        == 503
    )
    assert ha.call.await_count == 1
    assert ha.call.await_args.args[0] == "select"
    ha.states["select.luba_va_fixture0001_modalita_di_svolta"]["state"] = "unavailable"
    assert "ha_turning_mode" not in client.get("/api/jobs").json()["capability"]["fields"]


def test_ha_camera_refresh_tokens_cleanup_and_secrecy(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.services["mammotion"].update(
        {name: {} for name in ("refresh_stream", "get_tokens", "stop_video")}
    )
    data = {
        "appid": "app",
        "channelName": "channel",
        "uid": 42,
        "token": "media-canary",
        "openEncrypt": 1,
        "key": "key-canary",
        "salt": base64.b64encode(b"x" * 32).decode(),
    }
    ha.call = AsyncMock(
        side_effect=lambda domain, service, target, **kwargs: (
            {"service_response": data} if service == "get_tokens" else {}
        )
    )
    assert client.get("/api/state").json()["capabilities"]["camera"]["protocol"] == "agora"
    with client.websocket_connect("/api/camera") as ws:
        message = ws.receive_json()
        assert message["token"] == "media-canary"
        assert message["encryption"]["key"] == "key-canary"
        ws.send_json({"type": "renew"})
        assert ws.receive_json()["type"] == "agora"
    assert ha.call.await_args_list[0].args == (
        "mammotion",
        "refresh_stream",
        {"entity_id": "camera.luba"},
    )
    assert ha.call.await_args_list[-1].args[1] == "stop_video"
    assert "media-canary" not in client.get("/api/diagnostics").text
    assert "key-canary" not in client.get("/api/diagnostics").text


def test_empty_ha_subscription_explains_failure_and_stops(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.services["mammotion"].update(
        {name: {} for name in ("refresh_stream", "get_tokens", "stop_video")}
    )
    ha.call = AsyncMock(return_value={"service_response": {}})
    with client.websocket_connect("/api/camera") as ws:
        message = ws.receive_json()
        assert message["type"] == "error"
        assert "after refresh" in message["message"]
    assert ha.call.await_args_list[-1].args[1] == "stop_video"


def test_camera_rejects_incomplete_encryption():
    with pytest.raises(HAError, match="encryption"):
        agora_payload({"appid": "a", "channelName": "c", "token": "t", "openEncrypt": 1})
