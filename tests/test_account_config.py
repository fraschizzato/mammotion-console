import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.config import Settings, sanitize
from backend.logging_config import JsonFormatter
from backend.main import create_app


def test_account_secrets_are_env_only_and_redacted(monkeypatch):
    monkeypatch.setenv("CONNECTION_MODE", "mammotion")
    monkeypatch.setenv("MAMMOTION_USERNAME", "account-canary@example.test")
    monkeypatch.setenv("MAMMOTION_PASSWORD", "password-canary")
    monkeypatch.setenv("HA_TOKEN", "ha-canary")
    settings = Settings(_env_file=None)
    assert settings.connection_mode == "mammotion"
    assert settings.mammotion_password.get_secret_value() == "password-canary"
    text = " ".join(settings.redaction_secrets)
    cleaned = sanitize(
        {"message": [text], "refresh_token": "upstream-secret"}, settings.redaction_secrets
    )
    assert cleaned == {"message": ["[redacted] [redacted] [redacted]"]}
    record = logging.LogRecord("backend", logging.ERROR, "", 1, "%s", (text,), None)
    formatted = JsonFormatter(settings.redaction_secrets).format(record)
    for value in settings.redaction_secrets:
        assert value not in formatted
        assert value not in repr(settings)


def test_unknown_connection_mode_is_rejected():
    with pytest.raises(ValidationError):
        Settings(connection_mode="other", _env_file=None)


def test_sanitize_preserves_numeric_map_hash_keys():
    value = {9007199254742001: {"data": [{"x": 1.0, "y": 2.0}]}}

    assert sanitize(value) == value


def test_account_mode_never_silently_connects_to_ha(tmp_path):
    settings = Settings(connection_mode="mammotion", data_dir=tmp_path, _env_file=None)
    from backend.cloud import CloudClient

    app = create_app(settings)
    assert isinstance(app.state.ha, CloudClient)


def test_shared_map_calibration_is_env_configured_and_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("MAP_DISPLAY_ROTATION_DEGREES", "-18.5")
    monkeypatch.setenv("MAP_DISPLAY_OFFSET_EAST_M", "4.2")
    monkeypatch.setenv("MAP_DISPLAY_OFFSET_NORTH_M", "-1.5")
    monkeypatch.setenv("MAP_DISPLAY_ORIGIN_LATITUDE", "45.12345")
    monkeypatch.setenv("MAP_DISPLAY_ORIGIN_LONGITUDE", "10.54321")
    settings = Settings(data_dir=tmp_path, _env_file=None)
    with TestClient(create_app(settings)) as client:
        calibration = client.get("/api/settings").json()["server"]["map_calibration"]
    assert calibration == {
        "rotation_degrees": -18.5,
        "offset_east_m": 4.2,
        "offset_north_m": -1.5,
        "origin_latitude": 45.12345,
        "origin_longitude": 10.54321,
    }


def test_map_display_origin_requires_both_coordinates():
    with pytest.raises(ValidationError, match="MAP_DISPLAY_ORIGIN"):
        Settings(map_display_origin_latitude=45.12345, _env_file=None)


def test_blank_map_display_origin_env_uses_selected_map_centre(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("MAP_DISPLAY_ORIGIN_LATITUDE=\nMAP_DISPLAY_ORIGIN_LONGITUDE=\n")
    settings = Settings(_env_file=dotenv)
    assert settings.map_display_origin_latitude is None
    assert settings.map_display_origin_longitude is None
