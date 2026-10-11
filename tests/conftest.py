import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def map_a():
    return json.loads((FIXTURES / "map-a.json").read_text())


@pytest.fixture
def map_b():
    return json.loads((FIXTURES / "map-b.json").read_text())


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, ha_base_url="", ha_token="", _env_file=None))
    with TestClient(app, headers={"X-Console-Request": "1"}) as client:
        yield client


def seed_ha(ha):
    ha.connected = True
    ha.error = ""
    states = [
        {
            "entity_id": "lawn_mower.luba",
            "state": "docked",
            "attributes": {"friendly_name": "LUBA Garden", "supported_features": 7},
        },
        {
            "entity_id": "sensor.luba_battery",
            "state": "87",
            "attributes": {"friendly_name": "Battery", "unit_of_measurement": "%"},
        },
        {"entity_id": "sensor.luba_map_sync_status", "state": "synced", "attributes": {}},
        {
            "entity_id": "switch.luba_orchard",
            "state": "on",
            "attributes": {"friendly_name": "Orchard", "hash": 101},
        },
        {
            "entity_id": "button.luba_task",
            "state": "unknown",
            "attributes": {"friendly_name": "Morning mow", "task_id": "42"},
        },
        {"entity_id": "camera.luba", "state": "idle", "attributes": {}},
    ]
    ha.states = {s["entity_id"]: s for s in states}
    ha.registry = [
        {
            "entity_id": s["entity_id"],
            "device_id": "dev-1",
            "platform": "mammotion",
            "unique_id": s["entity_id"],
            "translation_key": "battery_percent"
            if "battery" in s["entity_id"]
            else s["entity_id"].split("luba_")[-1],
        }
        for s in states
    ]
    ha.devices = [
        {"id": "dev-1", "name": "Luba-fixture", "model": "LUBA 2", "sw_version": "fixture"}
    ]
    ha.services = {
        "lawn_mower": {"pause": {}, "start_mowing": {}, "dock": {}},
        "mammotion": {
            "cancel_job": {},
            "get_geojson": {},
            "get_map_data": {},
            "start_mow": {
                "fields": {
                    "areas": {"selector": {"entity": {"multiple": True}}},
                    "speed": {"selector": {"number": {"min": 0.2, "max": 1.2}}},
                    "modify": {"selector": {"boolean": {}}},
                }
            },
            "create_task": {
                "fields": {
                    "name": {"required": True, "selector": {"text": {}}},
                    "weeks": {"selector": {"object": {}}},
                }
            },
            "edit_task": {"fields": {"name": {"selector": {"text": {}}}}},
            "delete_task": {},
            "refresh_tasks": {},
            "start_task": {},
        },
    }


# Synthetic zone-map defaults for legacy test fixtures; never used at runtime.
import pytest as _pytest

@_pytest.fixture(autouse=True)
def _example_zones_for_tests(monkeypatch):
    import backend.zones as _zones
    monkeypatch.setattr(_zones, "DEFAULT_ZONES", [
        {"area_m2": area, "name": f"Example Zone {i:02d}"}
        for i, area in enumerate([789,1159,596,323,456,90,128,142,104,291,302,606,14], 1)
    ])
