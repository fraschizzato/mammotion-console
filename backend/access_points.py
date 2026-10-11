"""User-maintained AP locations, independent of immutable mower maps."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from backend.storage import Store


class AccessPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    ssid: str = Field(default="", max_length=32)
    bssid: str = Field(default="", pattern=r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$|^$")
    environment: Literal["indoor", "outdoor"] = "outdoor"
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


def access_point_routes(store: Store):
    router = APIRouter(prefix="/api/network/access-points")

    @router.get("")
    def listing():
        return store.documents("access_point")

    @router.post("")
    def create(body: AccessPoint):
        return store.put("access_point", body.model_dump())

    @router.put("/{key}")
    def update(key: UUID, body: AccessPoint):
        return store.put("access_point", body.model_dump(), str(key))

    @router.delete("/{key}")
    def delete(key: UUID):
        store.delete("access_point", str(key))
        return {"deleted": True}

    return router
