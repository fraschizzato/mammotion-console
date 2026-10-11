import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from pymammotion.data.model.device import MowingDevice
from pymammotion.data.model.device_limits import DeviceLimits, RangeLimit
from pymammotion.mammotion.commands.mammotion_command import MammotionCommand
from pymammotion.transport.base import TransportType

from backend.cloud import CloudClient
from backend.config import Settings
from backend.ha import HAError
from backend.main import create_app
from backend.movement import Movement


@pytest.fixture
def cloud(tmp_path):
    settings = Settings(connection_mode="mammotion", data_dir=tmp_path, _env_file=None)
    app = create_app(settings)
    with TestClient(app, headers={"X-Console-Request": "1"}) as browser:
        adapter = app.state.ha
        raw = MowingDevice(name="Luba-VTEST")
        raw.report_data.dev.sys_status = 11
        raw.report_data.dev.battery_val = 87
        raw.map.area[101] = Mock()
        transport = SimpleNamespace(is_connected=True, send=AsyncMock())
        handle = SimpleNamespace(
            device_name="Luba-VTEST",
            snapshot=SimpleNamespace(raw=raw),
            iot_id="device-iot",
            has_usable_transport=True,
            last_report_data_at=time.monotonic(),
            commands=MammotionCommand("Luba-VTEST", 1),
            get_transport=lambda kind: transport if kind == TransportType.CLOUD_MAMMOTION else None,
        )
        adapter.client = SimpleNamespace(
            device_registry=SimpleNamespace(all_devices=[handle]),
            mower=lambda name: handle,
            ensure_fresh_state=AsyncMock(),
            send_command_with_args=AsyncMock(),
            send_command_and_wait=AsyncMock(),
            stop=AsyncMock(),
            start_map_sync=AsyncMock(),
            regenerate_stale_geojson=Mock(),
            get_stream_subscription=AsyncMock(),
            stop_stream=AsyncMock(),
        )
        adapter.connected, adapter.error = True, ""
        yield browser, adapter, handle, transport


def test_cloud_telemetry_and_scoped_credentials(cloud):
    browser, adapter, handle, _ = cloud
    assert isinstance(adapter, CloudClient)
    from pymammotion.data.model.enums import RTKStatus

    handle.snapshot.raw.work.speed = 0.42
    handle.snapshot.raw.report_data.work.area = 24 << 16
    handle.snapshot.raw.report_data.work.progress = (36 << 16) | 90
    handle.snapshot.raw.report_data.dev.charge_state = 1
    handle.snapshot.raw.mowing_state.rtk_status = RTKStatus.FIX
    handle.snapshot.raw.mowing_state.satellites_total = 31
    handle.snapshot.raw.mowing_state.satellites_l2 = 18
    state = browser.get("/api/state").json()
    assert state["provider"] == "mammotion"
    assert state["online"]
    assert state["metrics"][0]["value"] == "87"
    metrics = {item["key"]: item["value"] for item in state["metrics"]}
    assert (
        metrics
        | {
            "mowing_speed": "0.42",
            "progress": "24",
            "total_time": "90",
            "elapsed_time": "54",
            "left_time": "36",
            "charging_state": "charging",
            "rtk_status": "fix",
            "satellites_total": "31",
            "satellites_l2": "18",
        }
        == metrics
    )
    assert browser.get("/api/health").json()["ha_connected"] is False
    from pydantic import SecretStr

    adapter.settings.mammotion_username = SecretStr("email-canary@example.test")
    adapter.settings.mammotion_password = SecretStr("password-canary")
    adapter.error = "email-canary@example.test password-canary"
    for path in ("/api/state", "/api/settings", "/api/diagnostics", "/api/jobs"):
        response = browser.get(path)
        assert response.status_code == 200, response.text
        assert "password-canary" not in response.text
        assert "email-canary" not in response.text
    assert browser.put("/api/settings", json={"mammotion_password": "new"}).status_code == 422
    handle.last_report_data_at = time.monotonic() - 200
    assert not browser.get("/api/state").json()["online"]
    assert browser.post("/api/mower/dock", json={"confirmed": True}).status_code == 503
    adapter.client.send_command_with_args.assert_not_awaited()


def test_cloud_selection_never_guesses_multiple_mowers(cloud):
    browser, adapter, handle, _ = cloud
    adapter.client.device_registry.all_devices.append(
        SimpleNamespace(device_name="Luba-OTHER", snapshot=handle.snapshot)
    )
    assert browser.get("/api/state").json()["device"] is None
    adapter.settings.mammotion_device = "Luba-VTEST"
    assert browser.get("/api/state").json()["device"]["device_name"] == "Luba-VTEST"


def test_cloud_control_and_unavailable_schedules(cloud):
    browser, adapter, handle, _ = cloud
    assert browser.post("/api/mower/resume", json={"confirmed": True}).status_code == 422
    handle.snapshot.raw.report_data.dev.sys_status = 19
    assert browser.post("/api/jobs/resume", json={"confirmed": True}).status_code == 200
    adapter.client.send_command_with_args.assert_awaited_once()
    assert adapter.client.send_command_with_args.call_args.args[1] == "resume_execute_task"
    assert not any(
        c["supported"] for c in browser.get("/api/schedules").json()["capabilities"].values()
    )


def test_cloud_map_requires_sync_evidence(cloud):
    browser, adapter, _, _ = cloud
    assert browser.post("/api/maps/capture", json={}).status_code == 503
    assert browser.get("/api/maps").json() == []


def test_unexpected_map_archive_failure_is_controlled_and_diagnostic(cloud, map_a, monkeypatch):
    browser, adapter, _, _ = cloud
    payload = dict(map_a)
    payload["source_metadata"] = {"provider": "pymammotion_cloud_cache"}
    adapter.complete_map = AsyncMock(return_value=payload)
    monkeypatch.setattr(
        browser.app.state.archive,
        "capture",
        Mock(side_effect=RuntimeError("sensitive upstream payload")),
    )

    response = browser.post("/api/maps/capture", json={})

    assert response.status_code == 503
    assert response.json() == {"detail": "Map capture failed; archive state was preserved"}
    event = browser.get("/api/events?kind=map_sync").json()[0]
    assert event["message"] == "Map capture failed; archive state was preserved"
    assert event["details"] == {"stage": "archive", "error_type": "RuntimeError"}
    assert "sensitive upstream payload" not in response.text
    assert "sensitive upstream payload" not in browser.get("/api/diagnostics").text


def test_cloud_zone_geometry_keeps_computed_area_separate_from_native_mapping(cloud):
    _, adapter, handle, _ = cloud
    zone_hash = "9007199254742001"
    side = 789**0.5
    handle.snapshot.raw = SimpleNamespace(
        map=SimpleNamespace(
            to_dict=lambda: {
                "area": {
                    zone_hash: {
                        "data": [
                            {
                                "current_frame": 1,
                                "data_couple": [
                                    {"x": 0, "y": 0},
                                    {"x": side, "y": 0},
                                    {"x": side, "y": side},
                                    {"x": 0, "y": side},
                                ],
                            }
                        ]
                    }
                },
                "area_name": [],
            },
            generated_geojson={},
        ),
        location=SimpleNamespace(
            to_dict=lambda: {
                "RTK": {"latitude": 43.0, "longitude": 11.0, "yaw": 0},
                "dock": {},
            }
        ),
        work=SimpleNamespace(),
    )

    details = adapter.zone_geometry_details()[zone_hash]
    assert details["geometry_area_m2"] == pytest.approx(789, abs=0.05)
    assert "area_m2" not in details
    assert "mapped_name" not in details
    assert len(details["geometry_fingerprint"]) == 64


def test_cloud_rejects_legacy_job_rain_setting_before_dispatch(cloud):
    browser, adapter, _, _ = cloud
    assert "rain_tactics" not in browser.get("/api/jobs").json()["capability"]["fields"]
    response = browser.post(
        "/api/jobs/start",
        json={"confirmed": True, "fields": {"areas": ["switch.zone_101"], "rain_tactics": 1}},
    )
    assert response.status_code == 422
    adapter.client.send_command_and_wait.assert_not_awaited()


def test_cloud_job_route_uses_explicit_zone_settings(cloud, monkeypatch):
    browser, adapter, handle, _ = cloud
    monkeypatch.setattr(
        MowingDevice,
        "device_limits",
        property(
            lambda self: DeviceLimits(
                working_speed=RangeLimit(0.2, 0.6),
                blade_height=RangeLimit(25, 70),
                path_spacing=RangeLimit(15, 35),
            )
        ),
    )
    fields = {
        "areas": ["switch.zone_101"],
        "speed": 0.4,
        "blade_height": 40,
        "channel_width": 25,
        "toward": 30,
    }
    response = browser.post("/api/jobs/start", json={"confirmed": True, "fields": fields})
    assert response.status_code == 200, response.text
    calls = adapter.client.send_command_and_wait.call_args_list
    route = calls[0].kwargs["generate_route_information"]
    assert (route.one_hashs, route.speed, route.blade_height, route.toward) == ([101], 0.4, 40, 30)
    assert calls[1].args[1] == "start_job"
    # Real dependency serializes the constructed command (not just a matching mock).
    assert handle.commands.generate_route_information(route)
    assert browser.get("/api/jobs").json()["history"][0]["fields"] == fields


def test_luba3_job_schema_and_route_include_supported_app_settings(cloud, monkeypatch):
    browser, adapter, handle, _ = cloud
    handle.device_name = "luba-vatest"
    handle.snapshot.raw.device_firmwares.device_version = "2.3.30.39"
    monkeypatch.setattr(
        MowingDevice,
        "device_limits",
        property(
            lambda self: DeviceLimits(
                working_speed=RangeLimit(0.2, 1.2),
                blade_height=RangeLimit(25, 70),
                path_spacing=RangeLimit(20, 35),
            )
        ),
    )

    schema = browser.get("/api/jobs").json()["capability"]["fields"]
    assert schema["auto_change_direction"]["selector"] == {"boolean": {}}
    assert schema["ride_boundary_distance"]["selector"] == {"boolean": {}}

    fields = {
        "areas": ["switch.zone_101"],
        "speed": 0.9,
        "blade_height": 60,
        "channel_width": 27,
        "mowing_laps": 1,
        "auto_change_direction": True,
        "ride_boundary_distance": True,
    }
    response = browser.post("/api/jobs/start", json={"confirmed": True, "fields": fields})
    assert response.status_code == 200, response.text
    route = adapter.client.send_command_and_wait.call_args_list[0].kwargs[
        "generate_route_information"
    ]
    assert route.auto_change_direction == 1
    assert route.ride_boundary_distance == 0.5

    work = handle.snapshot.raw.work
    work.job_id = 42
    work.zone_hashs = [101]
    work.auto_change_direction = True
    work.ride_boundary_distance = 0.5
    current = adapter.current_job()
    assert current["fields"]["auto_change_direction"] is True
    assert current["fields"]["ride_boundary_distance"] is True


def test_unsupported_models_hide_new_job_settings(cloud):
    browser, _, _, _ = cloud
    schema = browser.get("/api/jobs").json()["capability"]["fields"]
    assert "auto_change_direction" not in schema
    assert "ride_boundary_distance" not in schema


def test_edge_coverage_requires_perimeter_lap(cloud):
    browser, adapter, handle, _ = cloud
    handle.device_name = "luba-vatest"
    handle.snapshot.raw.device_firmwares.device_version = "2.3.30.39"

    response = browser.post(
        "/api/jobs/start",
        json={
            "confirmed": True,
            "fields": {
                "areas": ["switch.zone_101"],
                "mowing_laps": 0,
                "ride_boundary_distance": True,
            },
        },
    )
    assert response.status_code == 422
    adapter.client.send_command_and_wait.assert_not_awaited()


def test_direct_camera_scoped_token_and_cleanup(cloud):
    browser, adapter, _, _ = cloud
    adapter.client.get_stream_subscription.return_value = SimpleNamespace(
        data=SimpleNamespace(
            appid="app",
            channelName="channel",
            token="media-only",
            uid=123,
            openEncrypt=0,
            availableTime=90,
        )
    )
    with browser.websocket_connect("/api/camera") as ws:
        reply = ws.receive_json()
        assert reply == {
            "type": "agora",
            "appid": "app",
            "channel": "channel",
            "token": "media-only",
            "uid": 123,
            "available_seconds": 90,
        }
        ws.send_json({"type": "heartbeat"})
    adapter.client.stop_stream.assert_awaited_once_with("Luba-VTEST")
    assert "media-only" not in browser.get("/api/diagnostics").text
    assert "media-only" not in str(adapter.store.events())


def test_movement_disabled_and_transport_pinned(cloud):
    browser, adapter, _, transport = cloud
    assert browser.post("/api/movement/forward", json={"transport": "wifi"}).status_code == 503
    adapter.settings.allow_manual_movement = True
    assert browser.post("/api/movement/forward", json={"transport": "bluetooth"}).status_code == 503
    transport.send.assert_not_awaited()
    assert browser.post("/api/movement/forward", json={"transport": "wifi"}).status_code == 200
    assert transport.send.await_count == 2


async def test_nudge_cancellation_still_stops():
    movement = Movement()
    sent = []
    started = asyncio.Event()

    async def send(payload, **kwargs):
        sent.append(payload)
        started.set()

    commands = MammotionCommand("Luba-VTEST", 1)
    handle = SimpleNamespace(
        commands=commands, iot_id="id", snapshot=SimpleNamespace(raw=MowingDevice())
    )
    transport = SimpleNamespace(send=send)
    task = asyncio.create_task(movement.nudge(handle, transport, "forward"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await movement.stop()
    from pymammotion.proto import LubaMsg

    messages = [LubaMsg().parse(payload).driver.todev_devmotion_ctrl for payload in sent]
    assert len(messages) == 2
    assert messages[0].set_linear_speed > 0
    assert (messages[1].set_linear_speed, messages[1].set_angular_speed) == (0, 0)


async def test_failed_stop_latches_movement_fault():
    movement = Movement()
    transport = SimpleNamespace(send=AsyncMock(side_effect=[None, OSError("radio lost")]))
    handle = SimpleNamespace(
        commands=MammotionCommand("Luba-VTEST", 1),
        iot_id="id",
        snapshot=SimpleNamespace(raw=MowingDevice()),
    )
    with pytest.raises(HAError, match="physical stop"):
        await movement.nudge(handle, transport, "left")
    with pytest.raises(HAError, match="physical stop"):
        await movement.nudge(handle, transport, "right")
    assert transport.send.await_count == 2


async def test_release_during_freshness_check_prevents_late_movement(cloud):
    _, adapter, handle, transport = cloud
    adapter.settings.allow_manual_movement = True
    entered, released = asyncio.Event(), asyncio.Event()

    async def fresh():
        entered.set()
        await released.wait()
        return handle

    adapter.fresh = fresh
    pending = asyncio.create_task(adapter.move("forward", "wifi"))
    await entered.wait()
    await adapter.move("stop", "wifi")
    released.set()
    assert await pending == {"accepted": False, "stopped": True}
    transport.send.assert_not_awaited()


async def test_bluetooth_movement_never_uses_cloud_transport(cloud):
    _, adapter, handle, cloud_transport = cloud
    adapter.settings.allow_manual_movement = True
    bluetooth = SimpleNamespace(is_connected=True, send=AsyncMock())
    handle.get_transport = lambda kind: bluetooth if kind == TransportType.BLE else cloud_transport
    await adapter.move("left", "bluetooth")
    assert bluetooth.send.await_count == 2
    cloud_transport.send.assert_not_awaited()


async def test_account_login_uses_env_credentials_and_cleans_up(tmp_path, monkeypatch):
    from pymammotion.client import MammotionClient

    from backend.storage import Store

    login = AsyncMock()
    monkeypatch.setattr(MammotionClient, "login_and_initiate_cloud", login)
    adapter = CloudClient(
        Settings(
            data_dir=tmp_path,
            connection_mode="mammotion",
            mammotion_username="account-canary",
            mammotion_password="password-canary",
            _env_file=None,
        ),
        Store(tmp_path),
    )
    task = asyncio.create_task(adapter.run())
    for _ in range(100):
        if adapter.connected:
            break
        await asyncio.sleep(0.01)
    assert adapter.connected
    assert login.call_args.args == ("account-canary", "password-canary")
    assert "session" in login.call_args.kwargs
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.session.closed
    await adapter.close()


async def test_cloud_maps_survive_switch_and_restart(cloud, map_a, map_b):
    from backend.maps import MapArchive
    from backend.storage import Store

    _, adapter, handle, _ = cloud
    archive = MapArchive(adapter.store)

    def use(payload):
        raw = {kind: {} for kind in ("area", "obstacle", "path")}
        for feature in payload["geojson"]["features"]:
            properties = feature["properties"]
            if properties.get("type_name") in raw:
                raw[properties["type_name"]][properties["hash"]] = {
                    "total_frame": 1,
                    "data": [{"current_frame": 1}],
                }
        handle.snapshot.raw.map = SimpleNamespace(
            area=raw["area"],
            area_name=[],
            is_map_synced=lambda _: True,
            to_dict=lambda: raw,
            generated_geojson=payload["geojson"],
        )
        handle.snapshot.raw.report_data.locations = [SimpleNamespace(bol_hash=123)]

    use(map_a)
    first = archive.capture(await adapter.complete_map(), handle.device_name, "A")
    before = (archive.directory(first["id"]) / "normalized.geojson").read_bytes()
    use(map_b)
    archive.capture(await adapter.complete_map(), handle.device_name, "B")
    use(map_a)
    again = archive.capture(await adapter.complete_map(), handle.device_name, "A")
    assert again["unchanged"] and again["id"] == first["id"]
    reopened = MapArchive(Store(adapter.store.root))
    assert len(reopened.list()) == 2
    assert (reopened.directory(first["id"]) / "normalized.geojson").read_bytes() == before


async def test_stale_complete_cloud_map_is_archived_without_contacting_mower(cloud, map_a, monkeypatch):
    from backend.maps import MapArchive

    _, adapter, handle, _ = cloud
    raw = {kind: {} for kind in ("area", "obstacle", "path")}
    for feature in map_a["geojson"]["features"]:
        properties = feature["properties"]
        if properties.get("type_name") in raw:
            raw[properties["type_name"]][properties["hash"]] = {
                "total_frame": 1,
                "data": [{"current_frame": 1}],
            }
    handle.snapshot.raw.map = SimpleNamespace(
        area=raw["area"],
        area_name=[],
        is_map_synced=lambda _: True,
        to_dict=lambda: raw,
        generated_geojson=map_a["geojson"],
    )
    handle.snapshot.raw.report_data.locations = [SimpleNamespace(bol_hash=123)]
    # Use a positive monotonic timestamp independent of runner uptime.
    monkeypatch.setattr("backend.cloud.time.monotonic", lambda: 1000.0)
    handle.last_report_data_at = 800.0

    payload = await adapter.complete_map()
    captured = MapArchive(adapter.store).capture(payload, handle.device_name, "Cached")

    assert payload["frames_complete"] is True
    assert payload["source_metadata"]["provider"] == "pymammotion_cloud_cache"
    assert "stale" in payload["source_metadata"]["cache_warning"]
    assert captured["manifest"]["completeness"] == "complete"
    assert adapter.map_capture_diagnostics() == {
        "stale_complete_cache_supported": True,
        "telemetry_fresh_seconds": 120,
        "device_available": True,
        "report_age_seconds": pytest.approx(200, abs=2),
        "report_stale": True,
        "sdk_map_synced": True,
        "root_manifest_synced": False,
        "reported_bol_available": True,
        "computed_bol_available": False,
        "root_manifest_available": False,
        "bol_hash_matches": None,
        "incomplete_hash_count": None,
        "orphan_area_name_count": 0,
        "frames_complete": True,
        "zone_count": 2,
        "cached_capture_ready": True,
    }
    adapter.client.ensure_fresh_state.assert_not_awaited()
    adapter.client.start_map_sync.assert_not_awaited()
    adapter.client.regenerate_stale_geojson.assert_called_once_with(handle.device_name)


async def test_fresh_complete_cloud_map_is_archived_without_restarting_map_sync(cloud, map_a):
    from backend.maps import MapArchive

    _, adapter, handle, _ = cloud
    raw = {kind: {} for kind in ("area", "obstacle", "path")}
    for feature in map_a["geojson"]["features"]:
        properties = feature["properties"]
        if properties.get("type_name") in raw:
            raw[properties["type_name"]][properties["hash"]] = {
                "total_frame": 1,
                "data": [{"current_frame": 1}],
            }
    handle.snapshot.raw.map = SimpleNamespace(
        area=raw["area"],
        area_name=[],
        is_map_synced=lambda _: True,
        to_dict=lambda: raw,
        generated_geojson=map_a["geojson"],
    )
    handle.snapshot.raw.report_data.locations = [SimpleNamespace(bol_hash=123)]
    handle.last_report_data_at = time.monotonic()

    payload = await adapter.complete_map()
    captured = MapArchive(adapter.store).capture(payload, handle.device_name, "In memory")

    assert payload["source_metadata"]["provider"] == "pymammotion_cloud_memory"
    assert "cache_warning" not in payload["source_metadata"]
    assert captured["manifest"]["completeness"] == "complete"
    adapter.client.ensure_fresh_state.assert_not_awaited()
    adapter.client.start_map_sync.assert_not_awaited()


async def test_complete_map_rebuilds_invalid_sdk_geometry_from_complete_raw_frames(cloud):
    from backend.maps import MapArchive

    _, adapter, handle, _ = cloud
    raw_map = {
        "area": {
            101: {
                "total_frame": 1,
                "data": [
                    {
                        "current_frame": 1,
                        "data_couple": [
                            {"x": 0, "y": 0},
                            {"x": 10, "y": 0},
                            {"x": 10, "y": 10},
                            {"x": 0, "y": 10},
                        ],
                    }
                ],
            }
        },
        "area_name": [{"hash": 101, "name": "Recovered zone"}],
    }
    handle.snapshot.raw.map = SimpleNamespace(
        area=raw_map["area"],
        area_name=[],
        is_map_synced=lambda _: True,
        to_dict=lambda: raw_map,
        generated_geojson={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"type_name": "area", "hash": 101},
                    "geometry": {"type": "GeometryCollection", "geometries": []},
                }
            ],
        },
    )
    handle.snapshot.raw.location = SimpleNamespace(
        to_dict=lambda: {
            "RTK": {"latitude": 0.216, "longitude": 0.092, "yaw": 0},
            "dock": {},
        }
    )
    handle.snapshot.raw.report_data.locations = [SimpleNamespace(bol_hash=123)]
    handle.last_report_data_at = time.monotonic() - 200

    payload = await adapter.complete_map()
    captured = MapArchive(adapter.store).capture(payload, handle.device_name, "Recovered")

    assert payload["source_metadata"]["geometry_source"] == "local_raw_frames"
    assert payload["geojson"]["features"][0]["properties"]["Name"] == "Recovered zone"
    assert captured["manifest"]["completeness"] == "complete"


async def test_complete_map_rebuilds_sdk_geometry_missing_declared_static_feature(cloud):
    from backend.maps import MapArchive

    _, adapter, handle, _ = cloud
    raw_map = {
        "area": {
            101: {
                "total_frame": 1,
                "data": [
                    {
                        "current_frame": 1,
                        "data_couple": [
                            {"x": 0, "y": 0},
                            {"x": 10, "y": 0},
                            {"x": 10, "y": 10},
                            {"x": 0, "y": 10},
                        ],
                    }
                ],
            }
        },
        "path": {
            202: {
                "total_frame": 1,
                "data": [
                    {
                        "current_frame": 1,
                        "data_couple": [{"x": 5, "y": 5}],
                    }
                ],
            }
        },
        "area_name": [{"hash": 101, "name": "Recovered zone"}],
    }
    generated = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"type_name": "area", "hash": 101, "Name": "Recovered zone"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [0.092, 0.216],
                            [0.0921, 0.216],
                            [0.0921, 0.2161],
                            [0.092, 0.2161],
                            [0.092, 0.216],
                        ]
                    ],
                },
            }
        ],
    }
    handle.snapshot.raw.map = SimpleNamespace(
        area=raw_map["area"],
        area_name=[],
        is_map_synced=lambda _: True,
        to_dict=lambda: raw_map,
        generated_geojson=generated,
    )
    handle.snapshot.raw.location = SimpleNamespace(
        to_dict=lambda: {
            "RTK": {"latitude": 0.216, "longitude": 0.092, "yaw": 0},
            "dock": {},
        }
    )
    handle.snapshot.raw.report_data.locations = [SimpleNamespace(bol_hash=123)]
    handle.last_report_data_at = time.monotonic()

    payload = await adapter.complete_map()
    captured = MapArchive(adapter.store).capture(payload, handle.device_name, "Recovered static")

    assert payload["source_metadata"]["geometry_source"] == "pymammotion_generated_plus_raw_frames"
    assert {
        feature["properties"].get("type_name")
        for feature in payload["geojson"]["features"]
        if feature["properties"].get("type_name")
    } == {"area", "path"}
    assert captured["manifest"]["completeness"] == "complete"


async def test_orphan_area_name_does_not_block_complete_root_manifest(cloud, map_a):
    from pymammotion.data.model.hash_list import AreaHashNameList

    browser, adapter, handle, _ = cloud
    raw = {kind: {} for kind in ("area", "obstacle", "path")}
    for feature in map_a["geojson"]["features"]:
        properties = feature["properties"]
        if properties.get("type_name") in raw:
            raw[properties["type_name"]][properties["hash"]] = {
                "total_frame": 1,
                "data": [{"current_frame": 1}],
            }
    current_hashes = [int(key) for key in raw["area"]]
    raw["area_name"] = [
        {"hash": current_hashes[0], "name": "Current"},
        {"hash": 999999, "name": "Deleted area"},
    ]
    handle.snapshot.raw.map = SimpleNamespace(
        area=raw["area"],
        area_name=[
            AreaHashNameList(hash=current_hashes[0], name="Current"),
            AreaHashNameList(hash=999999, name="Deleted area"),
        ],
        root_hash_lists=[SimpleNamespace()],
        computed_bol_hash=123,
        find_incomplete_hashes=lambda _: [],
        is_map_synced=lambda _: False,
        to_dict=lambda: raw,
        generated_geojson=map_a["geojson"],
    )
    handle.snapshot.raw.report_data.locations = [SimpleNamespace(bol_hash=123)]
    handle.last_report_data_at = time.monotonic() - 200

    payload = await adapter.complete_map()
    diagnostics = adapter.map_capture_diagnostics()

    assert payload["expected_zone_ids"] == [str(value) for value in sorted(current_hashes)]
    assert payload["source_metadata"]["sync_evidence"] == "root_manifest"
    assert payload["source_metadata"]["ignored_orphan_area_names"] == 1
    assert diagnostics["sdk_map_synced"] is False
    assert diagnostics["root_manifest_synced"] is True
    assert diagnostics["reported_bol_available"] is True
    assert diagnostics["computed_bol_available"] is True
    assert diagnostics["root_manifest_available"] is True
    assert diagnostics["bol_hash_matches"] is True
    assert diagnostics["incomplete_hash_count"] == 0
    assert diagnostics["orphan_area_name_count"] == 1
    assert diagnostics["zone_count"] == 2
    assert diagnostics["cached_capture_ready"] is True
    adapter.client.ensure_fresh_state.assert_not_awaited()
    adapter.client.start_map_sync.assert_not_awaited()

    response = browser.post("/api/maps/capture", json={"name": "Current cached map"})
    assert response.status_code == 200, response.text
    captured = response.json()
    assert captured["unchanged"] is False
    assert captured["manifest"]["completeness"] == "complete"
    assert len(captured["manifest"]["zones"]) == 2
    assert captured["manifest"]["source_metadata"]["sync_evidence"] == "root_manifest"
    assert browser.get("/api/maps").json()[0]["id"] == captured["id"]


async def test_stale_incomplete_cloud_map_fails_without_contacting_mower(cloud):
    _, adapter, handle, _ = cloud
    handle.last_report_data_at = time.monotonic() - 200

    with pytest.raises(HAError, match="archive is unchanged"):
        await adapter.complete_map()

    diagnostics = adapter.map_capture_diagnostics()
    assert diagnostics["report_stale"] is True
    assert diagnostics["cached_capture_ready"] is False
    adapter.client.ensure_fresh_state.assert_not_awaited()
    adapter.client.start_map_sync.assert_not_awaited()


async def test_startup_import_failure_is_visible_without_secrets(tmp_path, monkeypatch):
    import builtins

    from backend.storage import Store

    adapter = CloudClient(
        Settings(
            data_dir=tmp_path,
            mammotion_username="user-canary",
            mammotion_password="password-canary",
            _env_file=None,
        ),
        Store(tmp_path),
    )
    original = builtins.__import__

    def fail_import(name, *args, **kwargs):
        if name == "pymammotion.client":
            raise ModuleNotFoundError("password-canary")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_import)
    await adapter.run()
    assert "initialization failed (ModuleNotFoundError)" in adapter.error
    assert "password-canary" not in adapter.error
    assert not adapter.connected
    await adapter.close()


def test_encrypted_camera_uses_scoped_key_and_salt(cloud):
    import base64

    browser, adapter, handle, _ = cloud
    handle.last_report_data_at = 0  # Camera login must not wait for movement freshness.
    salt = base64.b64encode(b"x" * 32).decode()
    adapter.client.get_stream_subscription.return_value = SimpleNamespace(
        data=SimpleNamespace(
            appid="app",
            channelName="channel",
            token="media-only",
            uid=123,
            openEncrypt=1,
            key="scoped-key",
            salt=salt,
            availableTime=90,
        )
    )
    with browser.websocket_connect("/api/camera") as ws:
        data = ws.receive_json()
        assert data["encryption"] == {"mode": "aes-256-gcm2", "key": "scoped-key", "salt": salt}
    assert "scoped-key" not in browser.get("/api/diagnostics").text
    adapter.client.ensure_fresh_state.assert_not_awaited()


def test_wake_sleeping_mower_without_mqtt_or_freshness_probe(cloud):
    browser, adapter, handle, _ = cloud
    handle.snapshot.raw.report_data.dev.sys_status = 48
    handle.has_usable_transport = False
    handle.last_report_data_at = 0
    adapter.client.wake_device = AsyncMock(return_value=True)
    state = browser.get("/api/state").json()
    assert state["state"] == "hibernating"
    assert not state["online"]
    assert state["capabilities"]["wake"]["supported"]
    assert not state["capabilities"]["resume"]["supported"]
    assert browser.post("/api/mower/wake", json={"confirmed": False}).status_code == 422
    result = browser.post("/api/mower/wake", json={"confirmed": True})
    assert result.status_code == 200, result.text
    assert result.json()["pending"]
    adapter.client.wake_device.assert_awaited_once_with(handle.device_name)
    adapter.client.ensure_fresh_state.assert_not_awaited()
    adapter.client.send_command_with_args.assert_not_awaited()
    assert browser.post("/api/mower/wake", json={"confirmed": True}).status_code == 503


def test_wake_rejects_awake_mower_and_cloud_refusal(cloud):
    browser, adapter, handle, _ = cloud
    adapter.client.wake_device = AsyncMock(return_value=False)
    assert browser.post("/api/mower/wake", json={"confirmed": True}).status_code == 503
    adapter.client.wake_device.assert_not_awaited()
    handle.snapshot.raw.report_data.dev.sys_status = 48
    assert browser.post("/api/mower/wake", json={"confirmed": True}).status_code == 503
    assert browser.get("/api/state").json()["state"] == "hibernating"
