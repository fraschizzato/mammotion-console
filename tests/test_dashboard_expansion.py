import asyncio
import io
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, UnidentifiedImageError

from backend.config import Settings, sanitize
from backend.notifications import Telegram
from backend.storage import Store
from backend.zones import resolve_zone_name
from tests.conftest import seed_ha


@pytest.mark.parametrize(
    "area,name",
    [
        (789, "Example Zone 01"),
        (788, "Example Zone 01"),
        (790.4, "Example Zone 01"),
        ("456", "Example Zone 05"),
        (605, "Example Zone 12"),
        (1000, None),
        (None, None),
        (True, None),
        ("nan", None),
        ("path", None),
    ],
)
def test_area_mapping(area, name):
    assert resolve_zone_name(area) == name


def test_ha_zone_and_state_match_export_case(client):
    ha = client.app.state.ha
    seed_ha(ha)
    ha.states["lawn_mower.luba"]["state"] = "mowing"
    ha.states["sensor.luba_zona"] = {
        "entity_id": "sensor.luba_zona",
        "state": "789",
        "attributes": {"unit_of_measurement": "m²"},
    }
    ha.registry.append(
        {
            "entity_id": "sensor.luba_zona",
            "device_id": "dev-1",
            "platform": "mammotion",
            "translation_key": "area",
        }
    )
    live = client.get("/api/state").json()
    assert live["zone_name"] == "Example Zone 01"
    assert live["area_m2"] == 789
    assert live["area_m2_raw"] == "789"
    assert live["capabilities"]["pause"]["supported"]
    assert not live["capabilities"]["resume"]["supported"]
    ha.request = AsyncMock(return_value={"state": "mowing"})
    ha.call = AsyncMock()
    assert client.post("/api/mower/resume", json={"confirmed": True}).status_code == 503
    ha.call.assert_not_awaited()
    ha.connected = False
    caps = client.get("/api/state").json()["capabilities"]
    assert not caps["camera"]["supported"]
    assert not caps["pause"]["supported"]


def test_mapping_settings_are_persistent_and_unambiguous(client):
    preferences = client.get("/api/settings").json()["preferences"]
    assert preferences["zone_mapping"] == []
    preferences["tiles"] = "street"
    preferences["zone_mapping"] = [{"area_m2": 789, "name": "Custom zone"}]
    assert (
        client.put("/api/settings", json=preferences).json()["preferences"]["tiles"] == "satellite"
    )
    assert (
        Store(client.app.state.store.root).preferences()["zone_mapping"][0]["name"] == "Custom zone"
    )
    preferences["zone_mapping"].append({"area_m2": 792, "name": "Ambiguous"})
    assert client.put("/api/settings", json=preferences).status_code == 422


def test_tasks_are_scoped_to_map(client, map_a, map_b):
    ha = client.app.state.ha
    seed_ha(ha)
    for payload in (map_a, map_b):
        payload["device"] = "Luba-fixture"
    first = client.app.state.archive.capture(map_a, "Luba-fixture", "A")
    second = client.app.state.archive.capture(map_b, "Luba-fixture", "B")
    body = {
        "name": "Task A",
        "map_id": first["id"],
        "fields": {"areas": ["switch.luba_orchard"], "speed": 0.3},
    }
    assert client.post("/api/jobs/presets", json=body).status_code == 200
    body["map_id"] = second["id"]
    assert client.post("/api/jobs/presets", json=body).status_code == 422


async def test_telegram_transition_dedup_authorization_and_expiry(tmp_path):
    store = Store(tmp_path)
    store.put(
        "settings",
        {"telegram_enabled": True, "telegram_chat_id": "123", "telegram_user_ids": [456]},
        "preferences",
    )
    live = {
        "provider": "mammotion",
        "device": {"entity_id": "mammotion.test", "name": "Test"},
        "online": True,
        "state": "mowing",
        "area_m2": 789,
        "zone_name": "Example Zone 01",
        "capabilities": {"resume": {"supported": True}},
        "metrics": [],
    }
    provider = SimpleNamespace(telemetry=lambda: live, control=AsyncMock())
    telegram = Telegram(
        Settings(data_dir=tmp_path, telegram_bot_token="secret-canary", _env_file=None),
        store,
        lambda: provider,
        asyncio.Lock(),
    )
    telegram.call = AsyncMock(return_value=[])
    telegram.snapshot = AsyncMock(return_value=None)
    await telegram.observe()
    telegram.call.assert_not_awaited()
    live["state"] = "paused"
    await telegram.observe()
    await asyncio.gather(*list(telegram.media_tasks))
    assert "Example Zone 01" in telegram.call.call_args_list[0].args[1]["text"]
    count = telegram.call.await_count
    await telegram.observe()
    assert telegram.call.await_count == count
    key = next(iter(telegram.pending))
    query = {"id": "query", "from": {"id": 999}, "message": {"chat": {"id": 123}}, "data": key}
    await telegram.callback(query)
    provider.control.assert_not_awaited()
    query["from"]["id"] = 456
    await telegram.callback(query)
    provider.control.assert_not_awaited()
    query["data"] = next(iter(telegram.pending))
    await telegram.callback(query)
    provider.control.assert_awaited_once_with("resume")
    await telegram.callback(query)
    assert provider.control.await_count == 1
    telegram.pending["old"] = (telegram.identity(live), time.monotonic() - 1000, True, 456)
    query["data"] = "old"
    await telegram.callback(query)
    assert provider.control.await_count == 1


def test_frame_validation_and_diagnostic_picture_redaction():
    image = Image.new("RGB", (20, 20), "green")
    output = io.BytesIO()
    image.save(output, "PNG")
    assert Telegram.validate_frame(output.getvalue()).startswith(b"\xff\xd8")
    with pytest.raises(UnidentifiedImageError):
        Telegram.validate_frame(b"not an image")
    assert "entity_picture" not in sanitize({"entity_picture": "/api/camera_proxy/x?token=private"})


async def test_esri_image_marks_position_without_real_network(tmp_path, monkeypatch):
    import httpx

    tile = io.BytesIO()
    Image.new("RGB", (256, 256), "green").save(tile, "PNG")
    original = httpx.AsyncClient

    async def respond(request):
        assert request.url.host == "server.arcgisonline.com"
        return httpx.Response(200, content=tile.getvalue())

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    telegram = Telegram(
        Settings(data_dir=tmp_path, _env_file=None), Store(tmp_path), lambda: None, asyncio.Lock()
    )
    content = await telegram.map_image(
        {"latitude": 45, "longitude": 9}, {"entity_id": "mower.test"}
    )
    image = Image.open(io.BytesIO(content))
    assert image.size == (512, 512)
    assert any(r > 170 and g < 90 and b < 90 for r, g, b in image.get_flattened_data())


async def test_telemetry_bursts_coalesce(client):
    ha = client.app.state.ha
    seed_ha(ha)
    queue = asyncio.Queue(maxsize=1)
    ha.listeners.add(queue)
    for _ in range(100):
        ha.publish()
    assert queue.empty()
    await asyncio.sleep(0.15)
    assert queue.qsize() == 1
    assert queue.get_nowait()["online"]
    ha.listeners.discard(queue)


async def test_autonomous_capture_is_scoped_and_reuses_recent_frame(tmp_path, monkeypatch):
    from backend import notifications

    live = {"provider": "mammotion", "device": {"entity_id": "mower.test"}}
    telegram = Telegram(
        Settings(data_dir=tmp_path, _env_file=None),
        Store(tmp_path),
        lambda: SimpleNamespace(telemetry=lambda: live),
        asyncio.Lock(),
    )
    capture = AsyncMock(return_value=b"fresh-frame")
    monkeypatch.setattr(notifications, "capture_frame", capture)
    identity = telegram.identity(live)
    assert await telegram.snapshot(identity) == b"fresh-frame"
    telegram.frame = (identity, time.monotonic(), b"viewer-frame")
    assert await telegram.snapshot(identity) == b"viewer-frame"
    assert capture.await_count == 1
    telegram.frame = None
    assert await telegram.snapshot(("home_assistant", "other")) is None
    assert capture.await_count == 1


async def test_slow_camera_does_not_block_state_notifications(tmp_path):
    store = Store(tmp_path)
    store.put("settings", {"telegram_enabled": True, "telegram_chat_id": "123"}, "preferences")
    live = {
        "provider": "mammotion",
        "device": {"entity_id": "mower.test", "name": "Test"},
        "online": True,
        "state": "mowing",
        "capabilities": {},
    }
    telegram = Telegram(
        Settings(data_dir=tmp_path, telegram_bot_token="canary", _env_file=None),
        store,
        lambda: SimpleNamespace(telemetry=lambda: live),
        asyncio.Lock(),
    )
    wait = asyncio.Event()

    async def slow(_identity):
        await wait.wait()
        return None

    telegram.snapshot = slow
    telegram.call = AsyncMock()
    await telegram.observe()
    live["state"] = "paused"
    async with asyncio.timeout(0.5):
        await telegram.observe()
    assert telegram.media_tasks
    assert telegram.call.call_args_list[0].args[0] == "sendMessage"
    await telegram.close()
    assert not telegram.media_tasks
