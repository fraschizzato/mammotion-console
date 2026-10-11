from backend.config import Settings
from backend.main import create_app
from fastapi.testclient import TestClient


def test_map_offsets_are_server_controlled(tmp_path):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, map_offset_lat=4.5, map_offset_lon=-2.25,
        connection_mode="home_assistant",
    )
    with TestClient(
        create_app(settings),
        headers={"X-Console-Request": "1"},
    ) as client:
        body = client.get("/api/settings").json()["preferences"]
        assert body["offset_lat"] == 4.5
        assert body["offset_lon"] == -2.25
        body["offset_lat"] = 99
        body["offset_lon"] = 99
        response = client.put("/api/settings", json=body)
        assert response.status_code == 200
        updated = client.get("/api/settings").json()["preferences"]
        assert (updated["offset_lat"], updated["offset_lon"]) == (4.5, -2.25)
        stored = client.app.state.store.preferences()
        assert "offset_lat" not in stored
        assert "offset_lon" not in stored
