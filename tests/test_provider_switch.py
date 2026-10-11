import asyncio
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from backend.cloud import CloudClient
from backend.config import Settings
from backend.ha import HaClient
from backend.main import create_app


def test_switch_provider_persists_and_updates_live_stream(tmp_path, monkeypatch):
    async def idle(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(HaClient, "run", idle)
    monkeypatch.setattr(CloudClient, "run", idle)
    settings = Settings(
        data_dir=tmp_path,
        ha_base_url="http://ha.test",
        ha_token="ha-canary",
        mammotion_username="user-canary",
        mammotion_password="password-canary",
        _env_file=None,
    )
    app = create_app(settings)
    with TestClient(app, headers={"X-Console-Request": "1"}) as browser:
        with browser.websocket_connect("/api/live") as ws:
            assert ws.receive_json()["provider"] == "home_assistant"
            result = browser.put("/api/connection", json={"mode": "mammotion"})
            assert result.status_code == 200
            assert "password-canary" not in result.text
            assert "ha-canary" not in result.text
            assert ws.receive_json()["provider"] == "mammotion"
            assert isinstance(app.state.ha, CloudClient)
            stop = AsyncMock(return_value={"stopped": True})
            app.state.ha.move = stop
            assert (
                browser.put("/api/connection", json={"mode": "home_assistant"}).status_code == 200
            )
            stop.assert_awaited_once_with("stop", "wifi")
            assert ws.receive_json()["provider"] == "home_assistant"
        browser.put("/api/connection", json={"mode": "mammotion"})
    reopened = create_app(settings.model_copy(update={"connection_mode": "home_assistant"}))
    with TestClient(reopened) as browser:
        assert browser.get("/api/health").json()["provider"] == "mammotion"


def test_unconfigured_or_invalid_provider_is_rejected(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, _env_file=None))
    with TestClient(app, headers={"X-Console-Request": "1"}) as browser:
        for mode in ("mammotion", "invalid"):
            assert browser.put("/api/connection", json={"mode": mode}).status_code == 422
        assert browser.get("/api/health").json()["provider"] == "home_assistant"
