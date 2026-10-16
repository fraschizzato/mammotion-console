import { useEffect, useState } from "react";
import { Wifi, Signal, Download } from "lucide-react";
import { api, type Live, type Snapshot } from "./api";
import { MapView, signalColors } from "./MapView";
import { t } from "./i18n";
import type { FeatureCollection, Point } from "geojson";
import { AccessPoints, type AccessPoint } from "./AccessPoints";

export type NetworkData = FeatureCollection<Point> & {
  device: string;
  devices: string[];
  total_samples: number;
  truncated: boolean;
  retention_days: number;
};

export function NetworkSignal({ live }: { live: Live }) {
  const network = live.network;
  return (
    <section className="network-signal" aria-label={t("Robot network signal")}>
      <span>
        {t("Reported connection")}:{" "}
        <strong>{t(network?.connection || "Unknown")}</strong>
      </span>
      <div className="signal-readings">
        {(["wifi", "cellular"] as const).map((radio) => {
          const signal = network?.signals[radio];
          const fresh =
            signal?.fresh && (live.connected || live.mqtt?.connected);
          const Icon = radio === "wifi" ? Wifi : Signal;
          return (
            <span
              className="signal-reading"
              key={radio}
              style={{
                color: signalColors[fresh ? signal.quality : "unknown"],
              }}
            >
              <Icon size={19} />
              <span className="signal-bars" aria-hidden="true">
                {[1, 2, 3, 4].map((bar) => (
                  <i
                    key={bar}
                    style={{
                      height: `${bar * 4}px`,
                      opacity:
                        fresh &&
                        bar <=
                          ["weak", "fair", "good", "excellent"].indexOf(
                            signal.quality,
                          ) +
                            1
                          ? 1
                          : 0.2,
                    }}
                  />
                ))}
              </span>
              <strong>{radio === "wifi" ? "Wi-Fi" : "3G/4G"}</strong>
              {signal?.dbm != null ? `${signal.dbm} dBm` : "—"}
              <small>
                {t(
                  fresh
                    ? signal.quality
                    : signal?.dbm != null
                      ? "Stale reading"
                      : "Not reported",
                )}
              </small>
            </span>
          );
        })}
      </div>
      <small>
        SSID: {network?.ssid || t("Not reported")} · BSSID:{" "}
        {network?.bssid || t("Not reported")}
      </small>
      <small>
        {t("Radio signal is not a measurement of command latency.")}
      </small>
    </section>
  );
}

export function NetworkHistory({
  live,
  maps,
  preferences,
}: {
  live: Live;
  maps: Snapshot[];
  preferences: any;
}) {
  const [radio, setRadio] = useState("wifi"),
    [days, setDays] = useState("7"),
    [device, setDevice] = useState("");
  const [points, setPoints] = useState<AccessPoint[]>([]);
  const [draft, setDraft] = useState<AccessPoint | null>(null);
  const [snapshot, setSnapshot] = useState(
    preferences.default_snapshot || maps[0]?.id || "",
  );
  const [data, setData] = useState<NetworkData | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setData(null);
    setError("");
    const load = () =>
      api<NetworkData>(
        `/network/history?radio=${radio}&days=${days}&device=${encodeURIComponent(device)}`,
      )
        .then((result) => {
          if (active) {
            setData(result);
            setError("");
          }
        })
        .catch((err) => {
          if (active) setError(err.message);
        });
    void load();
    const timer = setInterval(load, 30000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [radio, days, device]);
  return (
    <div className="network-page">
      <section className="panel">
        <h2>{t("Network coverage history")}</h2>
        <NetworkSignal live={live} />
        <p className="muted">
          {t(
            "Measured signal only: empty areas have no samples, not necessarily no coverage. Collection continues with the dashboard closed.",
          )}
        </p>
        <div className="network-filters">
          <label>
            {t("Radio")}
            <select value={radio} onChange={(e) => setRadio(e.target.value)}>
              <option value="wifi">Wi-Fi</option>
              <option value="cellular">3G/4G</option>
            </select>
          </label>
          <label>
            {t("Period")}
            <select value={days} onChange={(e) => setDays(e.target.value)}>
              {[1, 7, 30, 90].map((d) => (
                <option key={d} value={d}>
                  {d} {t("days")}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("Mower")}
            <select value={device} onChange={(e) => setDevice(e.target.value)}>
              <option value="">{t("Current mower")}</option>
              {data?.devices.map((d) => (
                <option key={d}>{d}</option>
              ))}
            </select>
          </label>
          <label>
            {t("Base map")}
            <select
              value={snapshot}
              onChange={(e) => setSnapshot(e.target.value)}
            >
              <option value="">{t("No archived base")}</option>
              {maps.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
          </label>
          <button
            disabled={!data?.features.length}
            onClick={() => {
              const url = URL.createObjectURL(
                new Blob([JSON.stringify(data)], {
                  type: "application/geo+json",
                }),
              );
              const a = document.createElement("a");
              a.href = url;
              a.download = `mammotion-network-${radio}-${days}d.geojson`;
              a.click();
              setTimeout(() => URL.revokeObjectURL(url), 1000);
            }}
          >
            <Download size={16} />
            {t("Export heat map")}
          </button>
        </div>
        <div className="signal-legend">
          {["excellent", "good", "fair", "weak"].map((q, i) => (
            <span key={q}>
              <i style={{ background: signalColors[q] }} />
              {t(q)} (
              {
                [
                  ...(radio === "wifi"
                    ? ["≥ −55", "−67…−55", "−75…−67", "< −75"]
                    : ["≥ −75", "−85…−75", "−95…−85", "< −95"]),
                ][i]
              }{" "}
              dBm)
            </span>
          ))}
        </div>
        <p>
          {data
            ? `${data.total_samples} ${t("samples")} · ${data.features.length} ${t("measured cells")}`
            : t("Loading network history…")}
        </p>
        <p className="muted">
          {t(
            "Indicative RSSI bands, averaged in geographic cells about 11 m north–south. History retained for 90 days. SSID and BSSID are shown only when reported.",
          )}
        </p>
        {data?.truncated && (
          <p role="status">
            {t("Showing the 5000 most recent cells. Choose a shorter period.")}
          </p>
        )}
        {data && !data.features.length && (
          <p role="status">
            {t(
              "No fresh geolocated signal samples yet. History starts after this update; stale or missing readings are excluded.",
            )}
          </p>
        )}
        {error && <p role="alert">{error}</p>}
      </section>
      <MapView
        key={`${device}:${radio}:${days}`}
        snapshot={maps.find((m) => m.id === snapshot)}
        live={live}
        preferences={preferences}
        network={data || undefined}
        networkMode
        accessPoints={points}
        draftAccessPoint={draft}
        onMapPoint={(latitude, longitude) =>
          setDraft({
            name: "AP",
            ssid: "",
            bssid: "",
            environment: "outdoor",
            ...draft,
            latitude,
            longitude,
          })
        }
      />
      <AccessPoints
        points={points}
        onChange={setPoints}
        draft={draft}
        setDraft={setDraft}
      />
    </div>
  );
}
