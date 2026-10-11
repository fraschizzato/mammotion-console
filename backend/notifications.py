"""Opt-in Telegram state notifications. No secrets or camera frames on disk."""

import asyncio
import io
import json
import logging
import math
import secrets
import time

import httpx
from PIL import Image, ImageDraw

from backend.camera_capture import capture_frame
from backend.maps import MapArchive


class Telegram:
    def __init__(self, settings, store, provider, command_lock):
        # httpx INFO messages include URL paths, and Telegram embeds the bot token there.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.settings, self.store, self.provider = settings, store, provider
        self.command_lock = command_lock
        self.error = ""
        self.poll_error = ""
        self.previous = None
        self.pending: dict = {}
        self.frame = None
        self.offset = 0
        self.capture_lock = asyncio.Lock()
        self.media_tasks: set[asyncio.Task] = set()

    def configured(self):
        prefs = self.store.preferences()
        return bool(
            self.settings.telegram_bot_token.get_secret_value() and prefs.get("telegram_chat_id")
        )

    def enabled(self):
        return self.configured() and self.store.preferences().get("telegram_enabled", False)

    async def call(self, method, data, photo=None):
        if not self.enabled():
            return None
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token.get_secret_value()}/{method}"
        async with httpx.AsyncClient(timeout=30) as client:
            if photo:
                fields = {
                    k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                    for k, v in data.items()
                }
                response = await client.post(
                    url, data=fields, files={"photo": ("image.jpg", photo, "image/jpeg")}
                )
            else:
                response = await client.post(url, json=data)
            if response.status_code != 200:
                raise ValueError(f"Telegram API status {response.status_code}")
            payload = response.json()
            if not payload.get("ok"):
                raise ValueError("Telegram rejected request")
            return payload.get("result")

    async def snapshot(self, identity):
        if self.frame and self.frame[0] == identity and time.monotonic() - self.frame[1] <= 15:
            return self.frame[2]
        url = self.settings.camera_snapshot_url.get_secret_value()
        if not url:
            if not self.settings.telegram_camera_capture or self.capture_lock.locked():
                return None
            async with self.capture_lock:
                if self.identity(self.provider().telemetry()) != identity:
                    return None
                result = await capture_frame()
                if self.identity(self.provider().telemetry()) != identity:
                    return None
                return result
        async with httpx.AsyncClient(timeout=8) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content = bytearray()
                async for part in response.aiter_bytes():
                    content.extend(part)
                    if len(content) > 2_000_000:
                        raise ValueError("Snapshot exceeds limit")
        return await asyncio.to_thread(self.validate_frame, bytes(content))

    @staticmethod
    def validate_frame(content):
        image = Image.open(io.BytesIO(content))
        if image.width * image.height > 4_000_000:
            raise ValueError("Frame dimensions exceed limit")
        image.load()
        image.thumbnail((960, 540))
        output = io.BytesIO()
        image.convert("RGB").save(output, "JPEG", quality=80)
        return output.getvalue()

    async def map_image(self, position, selected):
        lat, lon = float(position["latitude"]), float(position["longitude"])
        if not (math.isfinite(lat) and math.isfinite(lon) and abs(lat) < 85 and abs(lon) <= 180):
            raise ValueError("Invalid position")
        # One Esri tile with exact mower marker. No placeholder coordinates.
        zoom = 18
        x = (lon + 180) / 360 * 2**zoom
        y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2**zoom
        url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{zoom}/{int(y)}/{int(x)}"
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(url)
            response.raise_for_status()
        image = Image.open(io.BytesIO(response.content)).convert("RGB").resize((512, 512))
        draw = ImageDraw.Draw(image)
        archive = MapArchive(self.store)
        snapshots = archive.list()
        device = selected.get("device_name") or selected.get("entity_id") if selected else None
        snapshots = [s for s in snapshots if s["device"] == device]
        preferred = self.store.preferences().get("default_snapshot")
        snapshot = next(
            (s for s in snapshots if s["id"] == preferred), snapshots[0] if snapshots else None
        )
        if snapshot:
            geo = json.loads(
                (archive.directory(snapshot["id"]) / "normalized.geojson").read_bytes()
            )

            def pixel(coordinate):
                longitude, latitude = coordinate[:2]
                latitude = max(-85, min(85, latitude))
                tx = (longitude + 180) / 360 * 2**zoom
                ty = (1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2 * 2**zoom
                return ((tx - int(x)) * 512, (ty - int(y)) * 512)

            for feature in geo["features"]:
                if feature["properties"].get("kind") != "zone":
                    continue
                geometry = feature["geometry"]
                polygons = (
                    [geometry["coordinates"]]
                    if geometry["type"] == "Polygon"
                    else geometry["coordinates"]
                    if geometry["type"] == "MultiPolygon"
                    else []
                )
                for polygon in polygons:
                    for ring in polygon:
                        draw.line([pixel(point) for point in ring], fill="#b4e88d", width=3)
        px, py = (x % 1) * 512, (y % 1) * 512
        draw.ellipse((px - 8, py - 8, px + 8, py + 8), fill="red", outline="white", width=3)
        draw.rectangle((0, 492, 512, 512), fill="black")
        draw.text((4, 495), "Esri / Maxar / Earthstar Geographics", fill="white")
        output = io.BytesIO()
        image.save(output, "JPEG")
        return output.getvalue()

    def identity(self, live):
        return (live.get("provider"), (live.get("device") or {}).get("entity_id"))

    async def observe(self):
        if not self.enabled():
            self.previous = None
            self.pending.clear()
            return
        live = self.provider().telemetry()
        if not live.get("online"):
            self.previous = None
            self.pending.clear()
            return
        current = (self.identity(live), live["state"])
        previous, self.previous = self.previous, current
        if previous is None or previous[0] != current[0]:
            self.pending.clear()
            return
        if previous == current:
            return
        self.pending.clear()
        prefs = self.store.preferences()
        if prefs.get("telegram_pauses_only", True) and live["state"] not in {
            "paused",
            "error",
            "locked",
        }:
            return
        chat = prefs["telegram_chat_id"]
        text = f"{live['device']['name']}: {previous[1]} → {live['state']}\n{live.get('zone_name') or 'Zona sconosciuta'} ({live.get('area_m2')} m²)"
        errors = [
            str(m["value"])
            for m in live.get("metrics", [])
            if any(k in m["key"] for k in ("error", "errore"))
        ]
        if errors:
            text += "\nErrore riportato: " + "; ".join(errors)[:400]
        text += "\nLa causa della pausa non è deducibile dal solo cambio di stato."
        markup: dict = {"inline_keyboard": []}
        if (
            live["state"] == "paused"
            and live["capabilities"].get("resume", {}).get("supported")
            and prefs.get("telegram_user_ids")
        ):
            key = secrets.token_urlsafe(12)
            self.pending[key] = (current[0], time.monotonic(), False, None)
            markup["inline_keyboard"].append([{"text": "Riprendi…", "callback_data": key}])
        await self.call("sendMessage", {"chat_id": chat, "text": text, "reply_markup": markup})
        if len(self.media_tasks) < 3:
            task = asyncio.create_task(self.send_media(live, chat, current[0]))
            self.media_tasks.add(task)
            task.add_done_callback(self.media_tasks.discard)
        return True

    async def send_media(self, live, chat, identity):
        try:
            await self._send_media(live, chat, identity)
        except Exception:
            self.error = "Telegram media delivery failed; state notification was sent separately"

    async def _send_media(self, live, chat, identity):
        try:
            photo = await self.snapshot(identity)
            if photo:
                await self.call(
                    "sendPhoto",
                    {"chat_id": chat, "caption": "Camera: fotogramma recente (massimo 15 secondi)"},
                    photo,
                )
            else:
                await self.call(
                    "sendMessage",
                    {"chat_id": chat, "text": "Camera: nessun fotogramma recente disponibile."},
                )
        except Exception:
            await self.call(
                "sendMessage",
                {"chat_id": chat, "text": "Camera: acquisizione immagine non disponibile."},
            )
        if live.get("position"):
            try:
                image = await self.map_image(live["position"], live["device"])
                await self.call(
                    "sendPhoto",
                    {
                        "chat_id": chat,
                        "caption": f"Posizione mower · {live['position']['latitude']}, {live['position']['longitude']} · Esri",
                    },
                    image,
                )
            except Exception:
                await self.call(
                    "sendMessage",
                    {
                        "chat_id": chat,
                        "text": "Mappa Esri non disponibile; posizione: "
                        + str(live["position"].get("latitude"))
                        + ", "
                        + str(live["position"].get("longitude")),
                    },
                )

    async def close(self):
        tasks = list(self.media_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.media_tasks.clear()

    async def callback(self, query):
        prefs = self.store.preferences()
        uid = query.get("from", {}).get("id")
        chat = query.get("message", {}).get("chat", {}).get("id")
        key = query.get("data", "")
        item = self.pending.get(key)
        if (
            not self.enabled()
            or str(chat) != prefs.get("telegram_chat_id")
            or uid not in prefs.get("telegram_user_ids", [])
            or not item
        ):
            return
        identity, created, confirmed, owner = item
        async with self.command_lock:
            live = self.provider().telemetry()
            if (
                time.monotonic() - created > 900
                or self.identity(live) != identity
                or not live.get("online")
                or live["state"] != "paused"
            ):
                self.pending.pop(key, None)
                await self.call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": query["id"],
                        "text": "Richiesta scaduta o mower non più in pausa",
                    },
                )
                return
            self.pending.pop(key, None)
            if not confirmed:
                confirm = secrets.token_urlsafe(12)
                self.pending[confirm] = (identity, created, True, uid)
                await self.call(
                    "sendMessage",
                    {
                        "chat_id": chat,
                        "text": "Confermi di riprendere il lavoro del mower? Verifica che possa muoversi in sicurezza.",
                        "reply_markup": {
                            "inline_keyboard": [
                                [{"text": "Conferma ripresa", "callback_data": confirm}]
                            ]
                        },
                    },
                )
            elif owner == uid:
                await self.provider().control("resume")
                self.store.event("control", "Resume requested from authorized Telegram user")
            await self.call(
                "answerCallbackQuery",
                {
                    "callback_query_id": query["id"],
                    "text": "Conferma inviata" if not confirmed else "Ripresa richiesta",
                },
            )

    async def monitor(self):
        while True:
            try:
                if await self.observe():
                    self.error = ""
            except Exception:
                self.error = "Telegram notification failed; check bot, chat and connectivity"
            await asyncio.sleep(2)

    async def poll(self):
        while True:
            try:
                if self.enabled():
                    updates = await self.call(
                        "getUpdates",
                        {
                            "offset": self.offset,
                            "timeout": 20,
                            "allowed_updates": ["callback_query"],
                        },
                    )
                    self.poll_error = ""
                    for update in updates or []:
                        self.offset = update["update_id"] + 1
                        if "callback_query" in update:
                            await self.callback(update["callback_query"])
                else:
                    await asyncio.sleep(2)
            except Exception:
                self.poll_error = (
                    "Telegram polling failed; use a dedicated bot without an existing webhook"
                )
                await asyncio.sleep(15)
