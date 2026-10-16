import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from backend.connections import Connections
from backend.coverage import completed_segments, swept_area
from backend.ha import HaClient, HAError
from tests.conftest import seed_ha
from tests.test_cloud import cloud as cloud_fixture

cloud = cloud_fixture


def feature(key, coords):
    return {
        "type": "Feature",
        "properties": {"path_hash": key, "path_type": 0},
        "geometry": {"type": "LineString", "coordinates": coords},
    }


def test_completed_area_excludes_future_path_and_uses_meter_width():
    p = [[10, 45], [10.00001, 45], [10.00002, 45], [10.00003, 45]]
    full = {"features": [feature(1, p), feature(2, p), feature(3, p)]}
    remaining = {"features": [feature(2, p[2:]), feature(3, p)]}
    completed = completed_segments(full, remaining, 2, 2, [1, 2, 3])
    assert [f["properties"]["path_hash"] for f in completed["features"]] == [1, 2]
    assert completed["features"][1]["geometry"]["coordinates"] == p[:3]
    area = swept_area(completed, 0.25)
    assert area["features"][0]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert area["features"][0]["properties"]["area_m2"] == pytest.approx(0.59, abs=0.02)
    assert not completed_segments(full, remaining, 2, 0, [])["features"]
    assert not swept_area(completed, 0)["features"]


def test_sdk_094_completed_area_endpoint_with_real_map_models(cloud):
    import math

    from pymammotion.data.model.hash_list import CommDataCouple, MowPath, MowPathPacket

    browser, _, handle, _ = cloud
    raw = handle.snapshot.raw
    raw.location.RTK.latitude = math.radians(45)
    raw.location.RTK.longitude = math.radians(10)
    raw.work.channel_width = 25
    raw.work.job_id = 123
    raw.report_data.work.ub_path_hash = 77
    raw.report_data.work.real_path_num = 2 << 8
    raw.map.current_mow_path = {
        1: {
            1: MowPath(
                total_frame=1,
                path_packets=[
                    MowPathPacket(
                        path_hash=77,
                        path_type=0,
                        data_couple=[
                            CommDataCouple(x=0, y=0),
                            CommDataCouple(x=2, y=0),
                            CommDataCouple(x=4, y=0),
                        ],
                    )
                ],
            )
        }
    }
    response = browser.get("/api/overlay")
    assert response.status_code == 200, response.text
    feature = response.json()["mowed"]["features"][0]
    assert feature["properties"]["area_m2"] == pytest.approx(0.5, abs=0.02)
    raw.map.current_mow_path[1][1].total_frame = 2
    assert not browser.get("/api/overlay").json()["mowed"]["features"]


def test_secondary_mqtt_enriches_only_matching_mower_and_preserves_controls(cloud):
    _, adapter, handle, _ = cloud
    primary = HaClient(adapter.settings, adapter.store)
    seed_ha(primary)
    primary.devices[0]["name"] = handle.device_name
    primary.states["lawn_mower.luba"]["state"] = "paused"
    handle.snapshot.raw.location.device.latitude = 45
    handle.snapshot.raw.location.device.longitude = 10
    adapter.radio_reports[handle.device_name] = {"position_time": time.time()}
    bridge = Connections(adapter.settings, adapter.store, lambda: primary)
    bridge.secondary = adapter
    enriched = bridge.enrich(primary.telemetry())
    assert enriched["position_source"] == "mammotion_mqtt"
    assert enriched["position"]["latitude"] == 45
    assert enriched["state"] == "paused"
    assert enriched["capabilities"]["resume"]["supported"]
    adapter.radio_reports[handle.device_name]["position_time"] -= 130
    assert bridge.enrich(primary.telemetry())["position"] is None
    primary.devices[0]["name"] = "Luba-DIFFERENT"
    assert bridge.enrich(primary.telemetry())["position"] is None
    asyncio.run(primary.close())


def test_video_switch_does_not_switch_control_provider(cloud):
    browser, adapter, handle, _ = cloud
    bridge = browser.app.state.connections
    bridge.settings.ha_base_url = "http://ha.invalid"
    bridge.settings.ha_token = SecretStr("ha-canary")
    peer = HaClient(bridge.settings, bridge.store)
    seed_ha(peer)
    peer.devices[0]["name"] = handle.device_name
    peer.services["mammotion"].update(
        {key: {} for key in ("refresh_stream", "get_tokens", "stop_video")}
    )
    peer.call = AsyncMock(
        side_effect=lambda domain, service, target, **kw: (
            {
                "service_response": {
                    "appid": "app",
                    "channelName": "channel",
                    "uid": 1,
                    "token": "scoped-media",
                }
            }
            if service == "get_tokens"
            else {}
        )
    )
    bridge.secondary = peer
    response = browser.put("/api/camera/settings", json={"mode": "home_assistant"})
    assert response.status_code == 200, response.text
    assert browser.get("/api/state").json()["provider"] == "mammotion"
    with browser.websocket_connect("/api/camera") as ws:
        assert ws.receive_json()["token"] == "scoped-media"
    assert peer.call.await_args_list[-1].args[1] == "stop_video"
    adapter.client.get_stream_subscription.assert_not_awaited()
    assert bridge.mode() == "home_assistant"
    assert "ha-canary" not in browser.get("/api/state").text


async def test_sdk_state_subscription_publishes_new_position(cloud):
    _, adapter, handle, _ = cloud
    queue = asyncio.Queue(maxsize=1)
    adapter.listeners.add(queue)
    handle.snapshot.raw.location.device.latitude = 45.123
    handle.snapshot.raw.location.device.longitude = 10.321
    await adapter._changed(handle.snapshot)
    message = await asyncio.wait_for(queue.get(), 1)
    assert message["position"]["latitude"] == 45.123
    assert message["mqtt"]["connected"]
    adapter.listeners.discard(queue)


async def test_video_rejects_a_different_secondary_mower(cloud):
    _, adapter, _, _ = cloud
    primary = HaClient(adapter.settings, adapter.store)
    seed_ha(primary)
    bridge = Connections(adapter.settings, adapter.store, lambda: primary)
    bridge.store.put("camera_connection", {"mode": "mammotion"}, "active")
    bridge.secondary = adapter
    bridge.settings.mammotion_username = SecretStr("test-user")
    bridge.settings.mammotion_password = SecretStr("test-password")
    with pytest.raises(HAError, match="matching"):
        await bridge.video_provider()
    await primary.close()


async def test_sdk_094_stream_retry_never_stops_new_firmware_encoder(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import PropertyMock

    from pymammotion.client import MammotionClient

    # Exercise the pinned upstream implementation, with HTTP and commands intercepted.
    http = SimpleNamespace(
        get_stream_subscription=AsyncMock(
            side_effect=[None, SimpleNamespace(data={"token": "fixture"})]
        )
    )
    client = object.__new__(MammotionClient)
    raw = SimpleNamespace(report_data=SimpleNamespace(dev=SimpleNamespace(fpv_info=1)))
    handle = SimpleNamespace(snapshot=SimpleNamespace(raw=raw))
    client._device_registry = SimpleNamespace(get_by_name=lambda _: handle)
    client.send_command_with_args = AsyncMock()
    monkeypatch.setattr(MammotionClient, "mammotion_http", PropertyMock(return_value=http))
    result = await client.get_stream_subscription("Luba-VTEST", "fixture-id")
    assert result.data["token"] == "fixture"
    assert http.get_stream_subscription.await_count == 2
    client.send_command_with_args.assert_not_awaited()


def test_cold_mowing_overlay_fetch_is_bounded_and_never_plans(cloud):
    from types import SimpleNamespace

    from pymammotion.data.model.report_info import LocationData

    _, adapter, handle, _ = cloud
    raw = handle.snapshot.raw
    raw.report_data.dev.sys_status = 13
    raw.report_data.work.path_hash = 77
    raw.report_data.locations = [LocationData(bol_hash=123)]
    handle.queue = SimpleNamespace(is_saga_active=False)
    handle.has_queued_commands = lambda: False
    adapter.client.check_and_get_mow_path = AsyncMock()
    adapter._wait_for_overlay = AsyncMock(return_value=True)
    # Allow the first fetch even when CI runner uptime is below 120 seconds.
    adapter.overlay_attempt = time.monotonic() - 121

    async def check():
        adapter.request_overlay(handle)
        first = adapter.overlay_task
        assert first is not None
        adapter.request_overlay(handle)
        assert adapter.overlay_task is first
        await first
        adapter.request_overlay(handle)
        assert adapter.overlay_task is first

    asyncio.run(check())
    adapter.client.start_map_sync.assert_awaited_once_with(handle.device_name, skip_area_names=True)
    adapter.client.check_and_get_mow_path.assert_awaited_once_with(handle.device_name)
    assert adapter._wait_for_overlay.await_count == 2
    adapter.client.send_command_and_wait.assert_not_awaited()
    adapter.client.send_command_with_args.assert_not_awaited()


def test_route_diagnostics_explain_missing_live_geometry_without_fetching(cloud):
    browser, adapter, handle, _ = cloud
    handle.snapshot.raw.report_data.work.path_hash = 2163392998022310366
    route = browser.get("/api/diagnostics").json()["mowing_route"]
    assert route["route_hash"] == "2163392998022310366"
    assert route["fetch_status"] == "not_requested"
    assert route["route_segments"] == 0
    assert not route["map_matches"]
    assert not route["rtk_origin_available"]
    assert not route["fetch_running"]
    assert "latitude" not in route and "iot_id" not in route
    adapter.client.start_map_sync.assert_not_awaited()
    adapter.client.send_command_with_args.assert_not_awaited()


def test_route_fetch_records_missing_map_hash(cloud):
    _, adapter, handle, _ = cloud
    asyncio.run(adapter.fetch_overlay(handle))
    assert adapter.overlay_diagnostics()["fetch_status"] == "missing_map_hash"
    adapter.client.start_map_sync.assert_not_awaited()


def test_overlay_waits_for_enqueued_read_only_saga_completion(cloud):
    _, adapter, _, _ = cloud
    handle = SimpleNamespace()
    ready = False

    async def check():
        nonlocal ready

        def completed():
            nonlocal ready
            if not ready:
                ready = not handle.has_queued_commands()
            return ready

        # Supply a separate deterministic queue because completed() also polls it.
        calls = iter((True, True, False))
        handle.has_queued_commands = lambda: next(calls)
        assert await adapter._wait_for_overlay(handle, completed, timeout=1)

    asyncio.run(check())


def test_report_monitor_waits_for_saga_and_allows_sdk_second_attempt(cloud, monkeypatch):
    from types import SimpleNamespace

    _, adapter, handle, _ = cloud
    bridge = Connections(adapter.settings, adapter.store, lambda: adapter)
    monkeypatch.setattr(bridge, "configured", lambda mode: True)
    handle.queue = SimpleNamespace(is_saga_active=True)

    async def check():
        finished = asyncio.Event()

        async def sdk_retry(**kwargs):
            # Two SDK 5-second report waits plus transport overhead exceed the
            # former 10-second outer timeout; the second attempt must finish.
            await asyncio.sleep(10.2)
            finished.set()

        handle.start_report_stream = AsyncMock(side_effect=sdk_retry)
        viewer = asyncio.Queue()
        adapter.listeners.add(viewer)
        task = asyncio.create_task(bridge.monitor())
        try:
            await asyncio.sleep(1.1)
            handle.start_report_stream.assert_not_awaited()
            assert bridge.last_report_stream == 0
            handle.queue.is_saga_active = False
            await asyncio.wait_for(finished.wait(), 14)
            handle.start_report_stream.assert_awaited_once_with(duration_ms=60000)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            adapter.listeners.discard(viewer)

    asyncio.run(check())


def test_monitor_recovers_selection_failure_and_reports_actual_refresh(cloud, monkeypatch):
    _, adapter, handle, _ = cloud
    bridge = Connections(adapter.settings, adapter.store, lambda: adapter)
    monkeypatch.setattr(bridge, "configured", lambda mode: True)
    attempts = 0

    def ensure(mode):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private-session-canary")
        return adapter

    monkeypatch.setattr(bridge, "ensure", ensure)

    async def check():
        arrived = asyncio.Event()
        viewer = asyncio.Queue()
        adapter.listeners.add(viewer)

        async def refresh(**kwargs):
            handle.last_report_data_at += 1
            arrived.set()

        handle.start_report_stream = AsyncMock(side_effect=refresh)
        task = asyncio.create_task(bridge.monitor())
        try:
            await asyncio.wait_for(arrived.wait(), 4)
            diagnostic = bridge.monitor_diagnostics()
            assert diagnostic["running"]
            assert diagnostic["errors"] == 1
            assert diagnostic["last_result"] == "report_received"
            assert diagnostic["attempts"] == 1
            assert "private-session-canary" not in str(diagnostic)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            adapter.listeners.discard(viewer)
        assert not bridge.monitor_diagnostics()["running"]

    asyncio.run(check())


def test_monitor_distinguishes_viewers_and_request_without_new_data(cloud, monkeypatch):
    browser, adapter, handle, _ = cloud
    bridge = Connections(adapter.settings, adapter.store, lambda: adapter)
    monkeypatch.setattr(bridge, "configured", lambda mode: True)
    handle.start_report_stream = AsyncMock()

    async def check():
        await bridge.monitor_tick()
        assert bridge.monitor_diagnostics()["state"] == "no_viewers"
        handle.start_report_stream.assert_not_awaited()
        viewer = asyncio.Queue()
        adapter.listeners.add(viewer)
        try:
            await bridge.monitor_tick()
            assert bridge.monitor_diagnostics()["last_result"] == "sdk_returned_without_new_report"
            await bridge.monitor_tick()
            handle.start_report_stream.assert_awaited_once()
        finally:
            adapter.listeners.discard(viewer)

    asyncio.run(check())
    assert "mqtt_monitor" in browser.get("/api/diagnostics").json()


def test_sleeping_cloud_mower_adds_wake_to_matching_ha_without_online_telemetry(cloud):
    _, adapter, handle, _ = cloud
    primary = HaClient(adapter.settings, adapter.store)
    seed_ha(primary)
    primary.devices[0]["name"] = handle.device_name
    primary.states["lawn_mower.luba"]["state"] = "unavailable"
    handle.snapshot.raw.report_data.dev.sys_status = 48
    handle.last_report_data_at = 0
    handle.has_usable_transport = False
    adapter.client.wake_device = AsyncMock(return_value=True)
    bridge = Connections(adapter.settings, adapter.store, lambda: primary)
    bridge.secondary = adapter
    enriched = bridge.enrich(primary.telemetry())
    assert enriched["state"] == "hibernating"
    assert not enriched["online"]
    assert enriched["availability"] == "suspended"
    assert enriched["capabilities"]["wake"]["supported"]
    assert enriched["provider"] == "home_assistant"
    handle.snapshot.raw.report_data.dev.sys_status = 15
    offline = bridge.enrich(primary.telemetry())
    assert offline["state"] == "unavailable"
    assert offline["availability"] == "offline"
    assert offline["capabilities"]["wake"]["supported"]
    primary.devices[0]["name"] = "Other mower"
    assert not bridge.enrich(primary.telemetry())["capabilities"]["wake"]["supported"]
    asyncio.run(primary.close())
