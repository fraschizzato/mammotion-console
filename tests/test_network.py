import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from backend.network import NetworkHistory, dbm, ha_network, signals
from backend.storage import Store
from tests.test_cloud import cloud as cloud_fixture

cloud = cloud_fixture


def observation(timestamp):
    return {
        "online": True,
        "position": {"latitude": 45.00001, "longitude": 10.00001},
        "position_observed_at": timestamp,
        "network": signals(
            -81,
            -61,
            "WIFI",
            "home_assistant",
            {"wifi": timestamp, "cellular": timestamp},
            True,
            timestamp,
        ),
    }


def test_signal_units_and_staleness():
    for value in (0, 31, "unknown", "nan", float("inf"), None, True, -200):
        assert dbm(value) is None
    stamp = datetime.now(UTC).isoformat()
    network = ha_network(
        [
            {"key": "wifi_rssi", "value": "-81", "unit": "dBm", "observed_at": stamp},
            {"key": "mnet_rssi", "value": "31", "unit": "", "observed_at": stamp},
            {"key": "connect_type", "value": "WIFI"},
        ],
        True,
    )
    assert network["signals"]["wifi"]["quality"] == "weak"
    assert network["signals"]["wifi"]["fresh"]
    assert network["signals"]["cellular"]["dbm"] is None
    assert not ha_network([{"key": "wifi_rssi", "value": -60, "unit": "dBm"}], True)["signals"][
        "wifi"
    ]["fresh"]


def test_history_persists_deduplicates_filters_and_aggregates(tmp_path):
    store = Store(tmp_path)
    history = NetworkHistory(store)
    stamp = time.time() - 40
    assert history.record("robot-a", observation(stamp), stamp) == 2
    assert history.record("robot-a", observation(stamp), stamp + 1) == 0
    again = NetworkHistory(Store(tmp_path))
    assert again.record("robot-a", observation(stamp), stamp + 20) == 0
    assert again.record("robot-b", observation(stamp), stamp + 20) == 2
    second = observation(stamp + 30)
    second["network"]["signals"]["wifi"]["dbm"] = -61
    assert history.record("robot-a", second, stamp + 30) == 2
    result = again.history("robot-a", "wifi", 1)
    assert result["total_samples"] == 2
    assert len(result["features"]) == 1
    props = result["features"][0]["properties"]
    assert props["mean_dbm"] == -71 and props["min_dbm"] == -81
    assert again.history("robot-a", "cellular")["features"][0]["properties"]["mean_dbm"] == -61
    assert result["devices"] == ["robot-a", "robot-b"]


def test_rejects_stale_mismatched_invalid_and_offline_positions(tmp_path):
    history = NetworkHistory(Store(tmp_path))
    stamp = time.time()
    for patch in (
        {"online": False},
        {"position": None},
        {"position": {"latitude": float("nan"), "longitude": 10}},
        {"position": {"latitude": 0, "longitude": 0}},
        {"position_observed_at": stamp - 40},
        {"position_observed_at": stamp - 130},
        {"position_observed_at": stamp + 10},
    ):
        assert history.record("robot", observation(stamp) | patch, stamp) == 0
    assert history.record("robot", observation(stamp - 130), stamp) == 0
    assert history.history("robot")["total_samples"] == 0


def test_network_api_validation(client):
    for suffix in ("?days=0", "?days=91", "?radio=bluetooth"):
        assert client.get("/api/network/history" + suffix).status_code == 422
    assert client.get("/api/network/history").json()["features"] == []
    stamp = time.time()
    client.app.state.network_history.record("test-device", observation(stamp), stamp)
    response = client.get("/api/network/history?device=test-device&radio=wifi").json()
    assert response["total_samples"] == 1
    assert response["features"][0]["geometry"]["type"] == "Point"


def test_collection_runs_without_browser_viewer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.config import Settings
    from backend.ha import HaClient
    from backend.main import create_app

    monkeypatch.setattr(HaClient, "telemetry", lambda self: observation(time.time()))
    monkeypatch.setattr(
        HaClient, "selected", lambda self: {"device_name": "fixture", "device_id": "fixture"}
    )
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            ha_base_url="",
            ha_token="",
            mammotion_username="",
            mammotion_password="",
        )
    )
    with TestClient(app) as browser:
        for _ in range(30):
            result = browser.get("/api/network/history?device=fixture").json()
            if result["total_samples"]:
                break
            time.sleep(0.05)
        assert result["total_samples"] == 1
        assert not app.state.ha.listeners


def test_cloud_radio_requires_actual_connect_report_not_other_traffic(cloud):
    from pymammotion.proto import NetUsedType, ReportInfoData, RptConnectStatus, RptDevLocation

    browser, adapter, handle, _ = cloud

    async def report(data):
        await adapter._network_report(
            handle.device_name, SimpleNamespace(sys=SimpleNamespace(toapp_report_data=data))
        )

    assert not browser.get("/api/state").json()["network"]["signals"]["wifi"]["fresh"]
    asyncio.run(
        report(
            ReportInfoData(
                connect=RptConnectStatus(wifi_rssi=-80, mnet_rssi=-61, used_net=NetUsedType.MNET),
                locations=[RptDevLocation()],
            )
        )
    )
    value = browser.get("/api/state").json()
    assert value["network"]["connection"] == "3G/4G"
    assert value["network"]["signals"]["wifi"]["fresh"]
    observed = value["network"]["signals"]["wifi"]["observed_at"]
    asyncio.run(report(ReportInfoData()))
    assert browser.get("/api/state").json()["network"]["signals"]["wifi"]["observed_at"] == observed
    adapter.radio_reports[handle.device_name]["observed"] -= 130
    assert not browser.get("/api/state").json()["network"]["signals"]["wifi"]["fresh"]
