import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import serve

from backend.config import Settings
from backend.ha import HaClient
from backend.jobs import validate_fields
from backend.storage import Store
from tests.conftest import seed_ha


async def test_real_websocket_auth_events_disconnect_reconnect(tmp_path):
    attempts = 0

    async def handler(ws):
        nonlocal attempts
        attempts += 1
        await ws.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await ws.recv())
        assert auth["access_token"] == "test-token"
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.9.mock"}))
        for _ in range(3):
            cmd = json.loads(await ws.recv())
            await ws.send(
                json.dumps({"id": cmd["id"], "type": "result", "success": True, "result": []})
            )
        await ws.send(
            json.dumps(
                {
                    "type": "event",
                    "event": {
                        "data": {
                            "entity_id": "sensor.battery",
                            "new_state": {
                                "entity_id": "sensor.battery",
                                "state": "63",
                                "attributes": {},
                            },
                        }
                    },
                }
            )
        )
        await asyncio.sleep(0.1)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ha = HaClient(
            Settings(
                data_dir=tmp_path,
                ha_base_url=f"http://127.0.0.1:{port}",
                ha_token="test-token",
                _env_file=None,
            ),
            Store(tmp_path),
        )
        ha.bootstrap = AsyncMock()
        task = asyncio.create_task(ha.run())
        try:
            async with asyncio.timeout(8):
                while attempts < 2:
                    await asyncio.sleep(0.05)
            assert ha.states["sensor.battery"]["state"] == "63"
            assert ha.version == "2026.9.mock"
            assert ha.last_event
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await ha.http.aclose()


async def test_complete_map_uses_base_not_progress(tmp_path, map_a):
    ha = HaClient(Settings(data_dir=tmp_path, _env_file=None), Store(tmp_path))
    seed_ha(ha)
    raw = {
        "area": {str(i): {"total_frame": 1, "data": [{"current_frame": 1}]} for i in [101, 102]},
        "area_name": [],
    }
    ha.service_response = AsyncMock(side_effect=[map_a["geojson"], raw])
    ha.request = AsyncMock(return_value={"state": "synced"})
    result = await ha.complete_map()
    assert result["expected_zone_ids"] == ["101", "102"]
    assert [c.args[0] for c in ha.service_response.call_args_list] == [
        "get_geojson",
        "get_map_data",
    ]
    ha.states["sensor.luba_map_sync_status"]["state"] = "syncing"
    with pytest.raises(ValueError, match="syncing"):
        await ha.complete_map()
    await ha.http.aclose()


def test_validation_rejects_nan_bool_numbers_unknown_fields():
    schema = {"speed": {"selector": {"number": {"min": 0.2, "max": 1.2}}}}
    for value in [float("nan"), float("inf"), True, -1]:
        with pytest.raises(ValueError):
            validate_fields({"speed": value}, schema, {"speed"})
    with pytest.raises(ValueError):
        validate_fields({"arbitrary": "payload"}, schema, {"speed"})


def test_ha_position_requires_a_recent_tracker_report(tmp_path):
    ha = HaClient(Settings(data_dir=tmp_path, _env_file=None), Store(tmp_path))
    seed_ha(ha)
    tracker = {
        "entity_id": "device_tracker.luba_position",
        "state": "home",
        "attributes": {"latitude": 45.0, "longitude": 10.0},
        "last_reported": datetime.now(UTC).isoformat(),
    }
    ha.states[tracker["entity_id"]] = tracker
    ha.registry.append(
        {
            "entity_id": tracker["entity_id"],
            "device_id": "dev-1",
            "platform": "mammotion",
            "unique_id": "fixture-position",
            "translation_key": "position",
        }
    )
    assert ha.telemetry()["position_online"]
    tracker["last_reported"] = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
    stale = ha.telemetry()
    assert not stale["position_online"]
    assert stale["position"] == tracker["attributes"]


def test_ha_availability_distinguishes_online_suspended_offline_and_disconnected(tmp_path):
    ha = HaClient(Settings(data_dir=tmp_path, _env_file=None), Store(tmp_path))
    seed_ha(ha)
    assert ha.telemetry()["availability"] == "online"
    ha.states["lawn_mower.luba"]["state"] = "hibernating"
    suspended = ha.telemetry()
    assert suspended["availability"] == "suspended"
    assert not suspended["online"]
    ha.states["lawn_mower.luba"]["state"] = "unavailable"
    assert ha.telemetry()["availability"] == "offline"
    ha.connected = False
    assert ha.telemetry()["availability"] == "disconnected"
