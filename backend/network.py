"""Observed radio signal, separate from transport latency and camera connectivity."""

import math
import time
from datetime import datetime


def epoch(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def dbm(value):
    try:
        number = float(value)
        # Zero is the SDK's unset value; positive CSQ/bars are not dBm.
        return number if not isinstance(value, bool) and -127 <= number < 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def quality(value, radio):
    if value is None:
        return "unknown"
    limits = (-55, -67, -75) if radio == "wifi" else (-75, -85, -95)
    return next(
        (
            name
            for limit, name in zip(limits, ("excellent", "good", "fair"), strict=True)
            if value >= limit
        ),
        "weak",
    )


def signals(wifi, cellular, connection, source, observed, online, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    rows = {}
    for radio, value in (("wifi", wifi), ("cellular", cellular)):
        received = observed.get(radio)
        age = timestamp - received if received is not None else None
        value = dbm(value)
        fresh = bool(online and age is not None and 0 <= age <= 120 and value is not None)
        rows[radio] = {
            "dbm": value,
            "quality": quality(value, radio),
            "fresh": fresh,
            "observed_at": received,
            "source": source,
        }
    return {"connection": connection or "unknown", "source": source, "signals": rows}


def ha_network(metrics, online):
    by_key = {m["key"]: m for m in metrics}
    wifi, cell = by_key.get("wifi_rssi", {}), by_key.get("mnet_rssi", {})
    return signals(
        wifi.get("value") if wifi.get("unit") == "dBm" else None,
        cell.get("value") if cell.get("unit") == "dBm" else None,
        by_key.get("connect_type", {}).get("value"),
        "home_assistant",
        {"wifi": epoch(wifi.get("observed_at")), "cellular": epoch(cell.get("observed_at"))},
        online,
    ) | {
        "ssid": by_key.get("wifi_ssid", {}).get("value") or None,
        "bssid": by_key.get("wifi_bssid", {}).get("value") or None,
    }


class NetworkHistory:
    """Bounded local history, sampled without polling the mower or needing a viewer."""

    def __init__(self, store):
        self.store = store
        self.last_sample = {}
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS network_samples (
                    id INTEGER PRIMARY KEY, device TEXT NOT NULL, radio TEXT NOT NULL,
                    source TEXT NOT NULL, observed REAL NOT NULL, position_time REAL NOT NULL,
                    latitude REAL NOT NULL, longitude REAL NOT NULL, dbm REAL NOT NULL,
                    connection TEXT NOT NULL,
                    UNIQUE(device,radio,source,observed,position_time));
                CREATE INDEX IF NOT EXISTS network_time ON network_samples(device,radio,observed);
                CREATE INDEX IF NOT EXISTS network_retention ON network_samples(observed);
            """)

    def record(self, device, live, timestamp=None):
        timestamp = time.time() if timestamp is None else timestamp
        position = live.get("position") or {}
        observed_position = live.get("position_observed_at")
        network = live.get("network") or {}
        if not device or not live.get("position_online", live.get("online")):
            return 0
        try:
            lat, lon = float(position["latitude"]), float(position["longitude"])
            if not (
                math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180
            ):
                return 0
            if (
                lat == 0
                and lon == 0
                or observed_position is None
                or not 0 <= timestamp - observed_position <= 120
            ):
                return 0
        except (KeyError, ValueError, TypeError):
            return 0
        inserted = 0
        with self.store.connect() as db:
            for radio, signal in network.get("signals", {}).items():
                observed, value = signal.get("observed_at"), dbm(signal.get("dbm"))
                source = signal.get("source")
                key = (device, radio, source)
                if (
                    radio not in ("wifi", "cellular")
                    or source not in ("home_assistant", "mammotion")
                    or not signal.get("fresh")
                    or value is None
                    or observed is None
                    or not 0 <= timestamp - observed <= 120
                    or abs(observed - observed_position) > 30
                    or timestamp - self.last_sample.get(key, 0) < 15
                ):
                    continue
                result = db.execute(
                    """INSERT OR IGNORE INTO network_samples
                    (device,radio,source,observed,position_time,latitude,longitude,dbm,connection)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        device,
                        radio,
                        source,
                        observed,
                        observed_position,
                        lat,
                        lon,
                        value,
                        str(network.get("connection", "unknown"))[:60],
                    ),
                )
                inserted += result.rowcount
                self.last_sample[key] = timestamp
            db.execute("DELETE FROM network_samples WHERE observed < ?", (timestamp - 90 * 86400,))
        return inserted

    def history(self, device, radio="wifi", days=7):
        # Round to geographic cells (~11m north/south); no invented/interpolated coverage.
        with self.store.connect() as db:
            devices = [
                r[0]
                for r in db.execute("SELECT DISTINCT device FROM network_samples ORDER BY device")
            ]
            args = (device, radio, time.time() - days * 86400)
            total = db.execute(
                "SELECT COUNT(*) FROM network_samples WHERE device=? AND radio=? AND observed>=?",
                args,
            ).fetchone()[0]
            rows = db.execute(
                """SELECT ROUND(latitude,4) latitude, ROUND(longitude,4) longitude,
                COUNT(*) samples, AVG(dbm) mean_dbm, MIN(dbm) min_dbm, MAX(dbm) max_dbm,
                MAX(observed) last_observed FROM network_samples
                WHERE device=? AND radio=? AND observed>=?
                GROUP BY ROUND(latitude,4),ROUND(longitude,4)
                ORDER BY last_observed DESC LIMIT 5001""",
                args,
            ).fetchall()
        return {
            "type": "FeatureCollection",
            "device": device,
            "devices": devices,
            "radio": radio,
            "days": days,
            "total_samples": total,
            "truncated": len(rows) > 5000,
            "retention_days": 90,
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
                    "properties": {**dict(r), "quality": quality(r["mean_dbm"], radio)},
                }
                for r in rows[:5000]
            ],
        }
