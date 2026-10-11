import asyncio
import copy
import math
import time
from unittest.mock import AsyncMock, Mock

import pytest

from backend.maps import MapArchive
from backend.storage import Store
from tests.test_cloud import cloud as cloud_fixture

cloud = cloud_fixture


def test_capture_ignores_millimetre_station_jitter_after_restart(tmp_path, map_a):
    station = {
        "type": "Feature",
        "properties": {"kind": "dock", "source_id": "station-test"},
        "geometry": {"type": "Point", "coordinates": [10, 45]},
    }
    map_a["geojson"]["features"].append(station)
    archive = MapArchive(Store(tmp_path))
    first = archive.capture(map_a, "m", "Captured map")
    original = (archive.directory(first["id"]) / "normalized.geojson").read_bytes()
    station["geometry"]["coordinates"][1] += 0.001 / 111320
    assert MapArchive(Store(tmp_path)).capture(map_a, "m", "Captured map")["id"] == first["id"]
    assert (archive.directory(first["id"]) / "normalized.geojson").read_bytes() == original
    station["geometry"]["coordinates"][1] += 1 / 111320
    assert archive.capture(map_a, "m", "Captured map")["id"] != first["id"]


def test_access_points_validate_persist_edit_delete(client):
    body = {
        "name": "Garden AP",
        "environment": "outdoor",
        "ssid": "Garden",
        "bssid": "AA:BB:CC:DD:EE:FF",
        "latitude": 45,
        "longitude": 10,
    }
    created = client.post("/api/network/access-points", json=body)
    assert created.status_code == 200
    key = created.json()["id"]
    assert client.get("/api/network/access-points").json()[0]["bssid"] == body["bssid"]
    assert (
        client.put(
            f"/api/network/access-points/{key}", json=body | {"environment": "indoor"}
        ).status_code
        == 200
    )
    for bad in (
        {"bssid": "not-a-mac"},
        {"latitude": 91},
        {"environment": "unknown"},
        {"password": "no"},
    ):
        assert client.post("/api/network/access-points", json=body | bad).status_code == 422
    assert client.delete(f"/api/network/access-points/{key}").status_code == 200
    assert client.get("/api/network/access-points").json() == []


def test_current_route_reprojects_yaw_and_keeps_same_route_during_refetch(cloud):
    from pymammotion.data.model.hash_list import (
        CommDataCouple,
        MowPath,
        MowPathPacket,
        NavGetHashListData,
        RootHashList,
    )

    _, adapter, handle, _ = cloud
    raw = handle.snapshot.raw
    raw.location.RTK.latitude, raw.location.RTK.longitude = math.radians(45), math.radians(10)
    raw.map.root_hash_lists = [RootHashList(sub_cmd=3, data=[NavGetHashListData(data_couple=[77])])]
    raw.report_data.work.path_hash = raw.map.computed_path_hash
    raw.report_data.work.ub_path_hash = 77
    raw.map.current_mow_path = {
        1: {
            1: MowPath(
                total_frame=1,
                path_packets=[
                    MowPathPacket(
                        path_hash=77,
                        path_type=0,
                        path_cur=1,
                        path_total=1,
                        data_couple=[CommDataCouple(x=0, y=0), CommDataCouple(x=10, y=0)],
                    )
                ],
            )
        }
    }
    adapter.regenerate_route(handle)
    east = copy.deepcopy(adapter.route_layers["get_mow_progress_geojson"])
    assert east["features"]
    raw.location.RTK.yaw = math.pi / 2
    adapter.regenerate_route(handle)
    north = copy.deepcopy(adapter.route_layers["get_mow_progress_geojson"])
    e = east["features"][0]["geometry"]["coordinates"]
    n = north["features"][0]["geometry"]["coordinates"]
    assert abs(e[-1][0] - e[0][0]) > 0.0001
    assert abs(n[-1][1] - n[0][1]) > 0.00008
    assert n[-1][0] == pytest.approx(n[0][0])
    raw.map.current_mow_path[1][1].total_frame = 2
    adapter.regenerate_route(handle)
    assert adapter.route_layers["get_mow_progress_geojson"] == north
    raw.report_data.work.path_hash += 1
    adapter.regenerate_route(handle)
    assert adapter.route_layers == {}


def test_network_ssid_is_not_robot_mac(cloud):
    browser, _, handle, _ = cloud
    handle.snapshot.raw.mower_state.wifi_ssid = "Garden"
    handle.snapshot.raw.mower_state.wifi_mac = "AA:BB:CC:DD:EE:FF"
    network = browser.get("/api/state").json()["network"]
    assert network["ssid"] == "Garden"
    assert network["bssid"] is None


def test_position_requires_a_recent_location_report_and_uses_current_rtk(cloud):
    from pymammotion.data.model.report_info import LocationData

    browser, adapter, handle, _ = cloud
    raw = handle.snapshot.raw
    raw.location.RTK.latitude, raw.location.RTK.longitude = math.radians(45), math.radians(10)
    raw.report_data.locations = [LocationData(real_pos_x=100000, real_pos_y=0)]
    adapter.radio_reports[handle.device_name] = {"position_time": time.time()}
    east = browser.get("/api/state").json()
    assert east["position_online"]
    raw.location.RTK.yaw = math.pi / 2
    north = browser.get("/api/state").json()
    assert north["position"]["latitude"] > east["position"]["latitude"]
    adapter.radio_reports[handle.device_name]["position_time"] -= 130
    assert not browser.get("/api/state").json()["position_online"]


def test_current_zone_hash_overrides_total_job_area(cloud):
    from pymammotion.data.model.hash_list import AreaHashNameList

    browser, _, handle, _ = cloud
    raw = handle.snapshot.raw
    raw.report_data.work.area = 1657
    raw.location.work_zone = 456123
    raw.map.area_name = [AreaHashNameList(hash=456123, name="Example Zone 04")]
    state = browser.get("/api/state").json()
    assert state["area_m2"] == 1657
    assert state["zone_hash"] == "456123"
    assert state["zone_name"] == "Example Zone 04"
    assert state["zone_name_source"] == "device_hash"


def test_luba3_live_cut_path_drives_traveled_overlay_and_mowed_area(cloud):
    from pymammotion.data.model.hash_list import CommDataCouple

    browser, adapter, handle, _ = cloud
    raw = handle.snapshot.raw
    handle.device_name = "luba-va-fixture0001"
    handle.set_mow_path_fetch_enabled = Mock()
    raw.device_firmwares.device_version = "2.3.30.39"
    raw.report_data.dev.sys_status = 13
    raw.location.RTK.latitude = math.radians(45)
    raw.location.RTK.longitude = math.radians(10)
    raw.work.job_id = 123
    raw.work.channel_width = 27
    raw.report_data.work.path_hash = 456

    async def live_path(_device_name):
        raw.map.update_dynamics_line(
            [CommDataCouple(x=0, y=0), CommDataCouple(x=5, y=0), CommDataCouple(x=10, y=0)]
        )

    adapter.client.get_dynamics_line = AsyncMock(side_effect=live_path)
    adapter._wait_for_overlay = AsyncMock(return_value=True)
    adapter._track_dynamics_session(handle)
    key = adapter._dynamics_key(handle)
    asyncio.run(adapter.fetch_dynamics_line(handle, key))
    adapter.dynamics_key = key

    response = browser.get("/api/overlay")
    assert response.status_code == 200
    overlay = response.json()
    assert len(overlay["traveled"]["features"]) == 1
    assert overlay["traveled"]["features"][0]["properties"]["point_count"] == 3
    assert overlay["mowed"]["features"][0]["properties"]["source"] == "mammotion_type18"
    assert overlay["mowed"]["features"][0]["properties"]["area_m2"] == pytest.approx(2.7, abs=0.1)
    handle.set_mow_path_fetch_enabled.assert_called_with(value=False)


def test_mqtt_position_trace_fills_live_progress_when_type18_is_empty(cloud):
    from pymammotion.data.model.report_info import LocationData

    browser, adapter, handle, _ = cloud
    raw = handle.snapshot.raw
    raw.report_data.dev.sys_status = 13
    raw.location.RTK.latitude = math.radians(45)
    raw.location.RTK.longitude = math.radians(10)
    raw.work.channel_width = 27
    raw.report_data.locations = [LocationData(real_pos_x=0, real_pos_y=0)]
    handle.last_report_data_at = time.monotonic()
    adapter._track_dynamics_session(handle)
    adapter._record_position_trace(handle)
    raw.report_data.locations[0].real_pos_x = 50000
    handle.last_report_data_at += 2
    adapter._record_position_trace(handle)

    overlay = browser.get("/api/overlay").json()
    assert len(overlay["traveled"]["features"]) == 1
    assert overlay["traveled"]["features"][0]["properties"]["source"] == (
        "mammotion_mqtt_position_trace"
    )
    assert overlay["traveled"]["features"][0]["properties"]["point_count"] == 2
    assert overlay["mowed"]["features"][0]["properties"]["source"] == (
        "mammotion_mqtt_position_trace"
    )
    assert overlay["mowed"]["features"][0]["properties"]["area_m2"] == pytest.approx(1.35, abs=0.1)
    mqtt_area = overlay["mowed"]["features"][0]["properties"]["area_m2"]
    trace_coordinates = overlay["traveled"]["features"][0]["geometry"]["coordinates"]
    adapter.dynamics_key = adapter._dynamics_key(handle)
    adapter.dynamics_layer = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"point_count": 2},
                "geometry": {"type": "LineString", "coordinates": trace_coordinates},
            }
        ],
    }
    hybrid = browser.get("/api/overlay").json()
    assert len(hybrid["traveled"]["features"]) == 2
    assert hybrid["mowed"]["features"][0]["properties"]["source"] == (
        "mammotion_hybrid_live_progress"
    )
    assert hybrid["mowed"]["features"][0]["properties"]["area_m2"] >= mqtt_area


def test_luba3_coverage_survives_reconnect_before_lane_width_report(cloud):
    from pymammotion.data.model.report_info import LocationData

    browser, adapter, handle, _ = cloud
    raw = handle.snapshot.raw
    handle.device_name = "luba-va-fixture0001"
    raw.report_data.dev.sys_status = 13
    raw.location.RTK.latitude = math.radians(45)
    raw.location.RTK.longitude = math.radians(10)
    raw.work.channel_width = 0
    raw.report_data.locations = [LocationData(real_pos_x=0, real_pos_y=0)]
    handle.last_report_data_at = time.monotonic()
    adapter._track_dynamics_session(handle)
    adapter._record_position_trace(handle)
    raw.report_data.locations[0].real_pos_x = 50000
    handle.last_report_data_at += 2
    adapter._record_position_trace(handle)

    overlay = browser.get("/api/overlay").json()
    properties = overlay["mowed"]["features"][0]["properties"]
    assert properties["width_m"] == pytest.approx(0.27)
    assert properties["width_source"] == "luba_va_default"
    diagnostics = browser.get("/api/diagnostics").json()["mowing_route"]
    assert diagnostics["coverage_width_cm"] == 27
    assert diagnostics["coverage_width_source"] == "luba_va_default"


def test_luba3_state_update_disables_unstable_full_route_fetch(cloud):
    _, adapter, handle, _ = cloud
    handle.device_name = "luba-va-fixture0001"
    handle.set_mow_path_fetch_enabled = Mock()
    handle.snapshot.raw.device_firmwares.device_version = "2.3.30.39"
    handle.snapshot.raw.report_data.dev.sys_status = 13
    asyncio.run(adapter._changed(handle.snapshot))
    handle.set_mow_path_fetch_enabled.assert_called_once_with(value=False)


def test_luba3_lowercase_cloud_registry_name_keeps_dynamic_capability(cloud):
    _, adapter, handle, _ = cloud
    handle.device_name = "luba-va-fixture0001"
    handle.snapshot.raw.device_firmwares.device_version = ""
    handle.snapshot.raw.mower_state.swversion = "2.3.30.39"
    assert adapter._device_type(handle).name == "LUBA_VA"
    assert adapter._firmware_version(handle) == "2.3.30.39"
    assert adapter.supports_dynamics_line(handle)


def test_luba3_missing_cloud_firmware_uses_bounded_readonly_probe(cloud):
    _, adapter, handle, _ = cloud
    raw = handle.snapshot.raw
    handle.device_name = "luba-va-fixture0001"
    raw.device_firmwares.device_version = ""
    raw.mower_state.swversion = ""
    raw.update_check.current_version = "1.0.0.0"
    raw.device_version = ""
    assert adapter._firmware_version(handle) == ""
    assert adapter._dynamics_capability(handle) == (True, "luba_va_readonly_probe")
    raw.device_firmwares.device_version = "1.15.2.0"
    assert adapter._dynamics_capability(handle) == (False, "sdk_model_firmware")


def test_empty_type18_responses_defer_to_mqtt_trace_for_rest_of_session(cloud):
    _, adapter, handle, _ = cloud
    raw = handle.snapshot.raw
    handle.device_name = "luba-va-fixture0001"
    raw.report_data.dev.sys_status = 13
    raw.device_firmwares.device_version = ""
    raw.mower_state.swversion = ""
    raw.update_check.current_version = "1.0.0.0"
    adapter.client.get_dynamics_line = AsyncMock()
    adapter._wait_for_overlay = AsyncMock(return_value=False)
    adapter._track_dynamics_session(handle)
    key = adapter._dynamics_key(handle)
    asyncio.run(adapter.fetch_dynamics_line(handle, key))
    asyncio.run(adapter.fetch_dynamics_line(handle, key))
    assert adapter.dynamics_empty_responses == 2
    assert adapter.dynamics_status == "live_progress_empty_using_mqtt"
    adapter.dynamics_task = None
    adapter.dynamics_attempt = 0
    adapter.request_dynamics_line(handle)
    assert adapter.dynamics_task is None


def test_luba3_live_path_session_survives_recharge_and_resets_after_idle(cloud):
    _, adapter, handle, _ = cloud
    raw = handle.snapshot.raw
    handle.device_name = "luba-va-fixture0001"
    raw.work.job_id = 0  # Ad-hoc jobs explicitly have no stable SDK job id.
    raw.report_data.dev.sys_status = 13
    raw.report_data.work.path_hash = 100
    adapter._track_dynamics_session(handle)
    first_key = adapter._dynamics_key(handle)
    assert adapter.dynamics_status == "awaiting_live_progress"
    adapter.dynamics_key = first_key
    adapter.dynamics_layer = {"type": "FeatureCollection", "features": [{"type": "Feature"}]}

    raw.report_data.work.path_hash = 200
    adapter._track_dynamics_session(handle)
    assert adapter._dynamics_key(handle) == first_key
    assert adapter.dynamics_layer["features"]

    raw.report_data.dev.sys_status = 19
    adapter._track_dynamics_session(handle)
    assert adapter._dynamics_key(handle) == first_key

    raw.report_data.dev.sys_status = 15
    adapter._track_dynamics_session(handle)
    assert adapter.dynamics_status == "awaiting_live_progress"
    assert adapter.dynamics_key == first_key
    assert adapter.dynamics_layer["features"]

    raw.report_data.dev.sys_status = 10
    adapter._track_dynamics_session(handle)
    assert adapter.dynamics_status == "session_inactive"
    assert adapter.dynamics_key is None
    assert adapter.dynamics_layer == {}

    raw.report_data.dev.sys_status = 13
    adapter._track_dynamics_session(handle)
    assert adapter._dynamics_key(handle) != first_key
