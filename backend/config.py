from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    connection_mode: Literal["home_assistant", "mammotion"] = "home_assistant"
    mammotion_username: SecretStr = SecretStr("")
    mammotion_password: SecretStr = SecretStr("")
    mammotion_device: str = ""
    mqtt_telemetry_enabled: bool = True
    bluetooth_enabled: bool = False
    bluetooth_address: str = ""
    ha_base_url: str = ""
    ha_token: SecretStr = SecretStr("")
    ha_config_path: Path | None = None
    mower_entity: str = ""
    allow_manual_movement: bool = False
    auto_capture: bool = True
    capture_interval: int = 300
    map_offset_lat: float = 0.0  # Map display north/south translation in metres.
    map_offset_lon: float = 0.0  # Map display east/west translation in metres.
    frontend_dir: Path = Path("frontend/dist")
    app_origin: str = ""
    telegram_camera_capture: bool = True
    telegram_bot_token: SecretStr = SecretStr("")
    camera_snapshot_url: SecretStr = SecretStr("")

    @field_validator("camera_snapshot_url")
    @classmethod
    def valid_snapshot_url(cls, value):
        raw = value.get_secret_value()
        if raw:
            parsed = urlsplit(raw)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("CAMERA_SNAPSHOT_URL must be an HTTP(S) URL")
        return value

    @property
    def redaction_secrets(self) -> tuple[str, ...]:
        return tuple(
            value.get_secret_value()
            for value in (
                self.ha_token,
                self.mammotion_username,
                self.mammotion_password,
                self.telegram_bot_token,
                self.camera_snapshot_url,
            )
            if value.get_secret_value()
        )

    @field_validator("ha_base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        if value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("HA_BASE_URL must be an HTTP URL without credentials/query")
        return value.rstrip("/")


def sanitize(value, secret: str | tuple[str, ...] = ""):
    if isinstance(value, dict):
        return {
            k: sanitize(v, secret)
            for k, v in value.items()
            if not any(
                s in str(k).lower()
                for s in (
                    "token",
                    "password",
                    "secret",
                    "authorization",
                    "credential",
                    "entity_picture",
                    "access_key",
                )
            )
        }
    if isinstance(value, list):
        return [sanitize(v, secret) for v in value]
    if isinstance(value, str) and secret:
        for item in sorted((secret,) if isinstance(secret, str) else secret, key=len, reverse=True):
            if item:
                value = value.replace(item, "[redacted]")
    return value
