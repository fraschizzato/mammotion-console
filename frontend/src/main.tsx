import { t, useLanguage, setLanguage } from "./i18n";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  Battery,
  ArrowDown,
  ArrowUp,
  CalendarDays,
  Check,
  ChevronRight,
  Download,
  Gauge,
  Home,
  Leaf,
  Map,
  Plus,
  Radio,
  RefreshCw,
  Settings,
  Shield,
  Trash2,
  Video,
  Wifi,
  Zap,
} from "lucide-react";
import {
  api,
  emptyLive,
  networkFailureEvent,
  networkSuccessEvent,
  socketUrl,
  type Live,
  type Snapshot,
} from "./api";
import { MapView } from "./MapView";
import { DirectionPreview } from "./DirectionPreview";
import { NetworkHistory, NetworkSignal } from "./Network";
import { Cockpit } from "./Cockpit";
import { CapabilityNote, Confirm, Fields } from "./Forms";
import "./style.css";
import "@fontsource/dm-sans/400.css";
import "@fontsource/dm-sans/500.css";
import "@fontsource/dm-sans/600.css";
import "@fontsource/manrope/600.css";
import "@fontsource/manrope/700.css";

const navigation = [
  ["Overview", Home],
  ["Maps", Map],
  ["Jobs", Leaf],
  ["Schedules", CalendarDays],
  ["Drive", Video],
  ["Network", Wifi],
  ["Diagnostics", Activity],
  ["Settings", Settings],
] as const;
type Confirmation = {
  title: string;
  description: React.ReactNode;
  action: () => Promise<void>;
};
function mapLabel(m: Snapshot) {
  return `${m.name} · v${m.version} · ${m.created ? new Date(m.created).toLocaleString() : m.id.slice(0, 8)} · ${m.manifest.zones.length} ${t("zones")}`;
}
function App() {
  const language = useLanguage();
  const reloadOnActivity = useRef(false);
  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);
  useEffect(() => {
    const reloadGuard = "mammotion-console:network-reload-attempted";
    const failure = () => {
      reloadOnActivity.current = true;
    };
    const activity = () => {
      if (!reloadOnActivity.current || document.visibilityState !== "visible") {
        return;
      }
      reloadOnActivity.current = false;
      try {
        if (sessionStorage.getItem(reloadGuard)) return;
        sessionStorage.setItem(reloadGuard, "1");
      } catch {
        // Storage can be unavailable in private/embedded browser contexts.
      }
      window.location.reload();
    };
    const resetGuard = () => {
      reloadOnActivity.current = false;
      try {
        sessionStorage.removeItem(reloadGuard);
      } catch {
        // A failed cleanup must never prevent normal console use.
      }
    };
    window.addEventListener(networkFailureEvent, failure);
    window.addEventListener("pointerdown", activity, { capture: true });
    window.addEventListener("keydown", activity, { capture: true });
    window.addEventListener("touchstart", activity, { capture: true });
    window.addEventListener("online", resetGuard);
    window.addEventListener(networkSuccessEvent, resetGuard);
    return () => {
      window.removeEventListener(networkFailureEvent, failure);
      window.removeEventListener("pointerdown", activity, { capture: true });
      window.removeEventListener("keydown", activity, { capture: true });
      window.removeEventListener("touchstart", activity, { capture: true });
      window.removeEventListener("online", resetGuard);
      window.removeEventListener(networkSuccessEvent, resetGuard);
    };
  }, []);
  const [page, setPage] = useState("Overview"),
    [live, setLive] = useState<Live>(emptyLive),
    [maps, setMaps] = useState<Snapshot[]>([]),
    [selected, setSelected] = useState(""),
    [settings, setSettings] = useState<any>({
      preferences: {},
      server: {},
      mowers: [],
    }),
    [error, setError] = useState(""),
    [toast, setToast] = useState(""),
    [confirm, setConfirm] = useState<Confirmation | null>(null),
    [busy, setBusy] = useState(false),
    [events, setEvents] = useState<any[]>([]);
  const refresh = useCallback(async () => {
    try {
      const [m, s, e] = await Promise.all([
        api<Snapshot[]>("/maps"),
        api("/settings"),
        api("/events"),
      ]);
      setMaps(m);
      setSettings(s);
      setEvents(e);
      setSelected((old) =>
        m.some((x) => x.id === old)
          ? old
          : s.preferences.default_snapshot || m[0]?.id || "",
      );
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void refresh();
    let closed = false;
    let timer: ReturnType<typeof setTimeout>;
    let ws: WebSocket;
    const connect = () => {
      ws = new WebSocket(socketUrl("/live"));
      ws.onmessage = (e) => {
        const message = JSON.parse(e.data);
        if (message.type === "map_snapshot_created") {
          if (message.snapshot_id) setSelected(message.snapshot_id);
          void refresh();
        } else setLive(message);
      };
      ws.onclose = () => {
        setLive((s) => ({
          ...s,
          connected: false,
          online: false,
          error: "Console disconnected; reconnecting…",
        }));
        if (!closed) timer = setTimeout(connect, 3000);
      };
    };
    connect();
    const poll = setInterval(refresh, 30000);
    return () => {
      closed = true;
      clearTimeout(timer);
      clearInterval(poll);
      ws.close();
    };
  }, [refresh]);
  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(""), 5000);
      return () => clearTimeout(timer);
    }
  }, [toast]);
  const snapshot = maps.find((m) => m.id === selected);
  const mapPreferences = useMemo(
    () => ({
      ...settings.preferences,
      map_calibration: settings.server.map_calibration,
    }),
    [settings.preferences, settings.server.map_calibration],
  );
  const perform = async (action: () => Promise<unknown>, message = "Saved") => {
    setBusy(true);
    try {
      await action();
      setToast(message);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const ask = (
    title: string,
    description: React.ReactNode,
    action: () => Promise<unknown>,
  ) =>
    setConfirm({
      title,
      description,
      action: async () => {
        await action();
        await refresh();
        setToast("Request completed");
      },
    });
  const control = (action: string) =>
    ask(
      action === "wake"
        ? t("Wake mower?")
        : `${action[0].toUpperCase() + action.slice(1)} mower?`,
      <p>
        {t("This sends a")} {t(action)} {t("command to")}{" "}
        <strong>{live.device?.name}</strong>.{" "}
        {action === "stop" &&
          "This cancels the mowing job; it is not a hardware emergency stop."}
      </p>,
      () => api(`/mower/${action}`, "POST", { confirmed: true }),
    );
  const metric = (key: string) => live.metrics.find((m) => m.key.endsWith(key));
  const availability =
    live.availability ??
    (live.state === "hibernating"
      ? "suspended"
      : live.online
        ? "online"
        : live.connected
          ? "offline"
          : "disconnected");
  const wakeAvailable = Boolean(live.capabilities.wake?.supported);
  return (
    <div className="app">
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setPage("Overview");
          }}
        >
          <span className="brand-icon">
            <Leaf size={25} />
          </span>
          <span>
            MAMMOTION<small>{t("FIELD CONSOLE")}</small>
          </span>
        </a>
        <div className="workspace-label">{t("YOUR WORKSPACE")}</div>
        <nav>
          {navigation.map(([name, Icon]) => (
            <button
              key={name}
              className={page === name ? "active" : ""}
              onClick={() => setPage(name)}
            >
              <Icon size={19} />
              <span>{t(name)}</span>
              {page === name && <ChevronRight size={15} />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-badge">
            <Shield size={17} />
            <div>
              {t("Local by design")}
              <small>{t("Maps stay on your server")}</small>
            </div>
          </div>
          <span className="version">Mammotion Console · 1.0</span>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <span>
            <span className="muted">{t("Workspace")}</span>
            <ChevronRight size={14} />
            {t(page)}
          </span>
          <div>
            <select
              className="language-select"
              aria-label={t("Language")}
              value={language}
              onChange={(event) =>
                setLanguage(event.target.value === "it" ? "it" : "en")
              }
            >
              <option value="en">English</option>
              <option value="it">Italiano</option>
            </select>
            <span className={`connection ${live.connected ? "online" : ""}`}>
              <i />
              {live.connected
                ? `${live.provider_label || "Home Assistant"} ${t("connected")}`
                : `${live.provider_label || "Home Assistant"} ${t("offline")}`}
            </span>
            <button
              className="icon-button"
              aria-label={t("Refresh console")}
              onClick={() => void refresh()}
            >
              <RefreshCw size={17} />
            </button>
          </div>
        </header>
        <main>
          <div className="page-title">
            <div>
              <span className="eyebrow">
                MAMMOTION /{" "}
                {page === "Overview"
                  ? t("PROPERTY AT A GLANCE")
                  : t(page).toUpperCase()}
              </span>
              <h1>
                {page === "Overview"
                  ? t("A little closer to your lawn.")
                  : page === "Maps"
                    ? t("Your maps. Always here.")
                    : t(page)}
              </h1>
              <p>
                {page === "Overview"
                  ? t(
                      "Live awareness. Precise control. A permanent record of your property.",
                    )
                  : page === "Maps"
                    ? t(
                        "Complete property maps, preserved independently of the cloud.",
                      )
                    : `${t("Connected through")} ${live.provider_label || "Home Assistant"}. ${t("Stored locally where it matters.")}`}
              </p>
            </div>
            <span className="date">
              {new Date().toLocaleDateString(language, {
                month: "short",
                day: "numeric",
                year: "numeric",
              })}
            </span>
          </div>
          {error && (
            <div className="error-banner" role="alert">
              {error}
              <button onClick={() => setError("")}>{t("Dismiss")}</button>
            </div>
          )}
          {!live.connected && (
            <div className="offline-banner">
              <Wifi size={17} />
              <span>
                {live.error || t("Mower connection is offline.")}{" "}
                {t("Your archived maps remain available.")}
              </span>
              <button onClick={() => setPage("Settings")}>
                {t("Connection settings")} <ChevronRight size={14} />
              </button>
            </div>
          )}
          {page === "Overview" && (
            <>
              <div className="overview-grid">
                <div className="overview-map">
                  <MapView
                    snapshot={snapshot}
                    live={live}
                    preferences={mapPreferences}
                  />
                  <div className="property-footer">
                    <div>
                      <span className="eyebrow">{t("PROPERTY MAP")}</span>
                      <strong>
                        {snapshot?.name || t("No map captured yet")}
                      </strong>
                    </div>
                    <button onClick={() => setPage("Maps")}>
                      {t("Open Map Library")} <ChevronRight size={16} />
                    </button>
                  </div>
                </div>
                <section className="panel mower-card">
                  <div className="section-title">
                    <span className="eyebrow">{t("YOUR MOWER")}</span>
                    <span
                      className={`pill ${
                        availability === "online"
                          ? "green"
                          : availability === "suspended"
                            ? "amber"
                            : ""
                      }`}
                    >
                      {availability === "suspended"
                        ? t("Suspended")
                        : availability === "online"
                          ? t("Online")
                          : availability === "disconnected"
                            ? t("Provider disconnected")
                            : t("Offline")}
                    </span>
                  </div>
                  <h2>{live.device?.name || t("Connect your mower")}</h2>
                  <p className="muted">
                    {live.device?.model ||
                      t("Connect your LUBA in server configuration")}
                  </p>
                  <figure className="mower-photo">
                    <img
                      src="/images/robot-mower.svg"
                      alt="Illustration of a robotic lawn mower"
                      width="600"
                      height="600"
                    />
                    <figcaption>
                      <a
                        href="https://eu.mammotion.com/products/luba-3-awd-robot-lawn-mower"
                        target="_blank"
                        rel="noreferrer"
                      >
                        Mammotion LUBA 3 AWD
                      </a>
                    </figcaption>
                  </figure>
                  <div className="state-row">
                    <Radio size={17} />
                    <strong>
                      {availability === "suspended"
                        ? t("Suspended — wake required")
                        : availability === "online"
                          ? t(live.state).replaceAll("_", " ")
                          : availability === "disconnected"
                            ? t("Provider disconnected")
                            : t("Offline")}
                    </strong>
                    <span aria-label={t("Battery level")}>
                      <Battery size={20} /> {t("Battery")}{" "}
                      {metric("battery_percent")?.value || "—"}%
                    </span>
                  </div>
                  <div
                    className="battery-track"
                    role="progressbar"
                    aria-label={t("Battery charge")}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={
                      Number(metric("battery_percent")?.value) || 0
                    }
                  >
                    <div
                      style={{
                        width: `${Math.max(0, Math.min(100, Number(metric("battery_percent")?.value) || 0))}%`,
                      }}
                    />
                  </div>
                  <NetworkSignal live={live} />
                  <div className="mini-stats">
                    <div>
                      <span>{t("Job progress")}</span>
                      <strong>
                        {metric("progress")?.value || "—"}
                        <small> %</small>
                      </strong>
                    </div>
                    <div>
                      <span>{t("Current zone")}</span>
                      <strong>{live.zone_name || "Zona sconosciuta"}</strong>
                      <small>
                        {live.area_m2 != null
                          ? `${live.area_m2} m²`
                          : "Area non disponibile"}
                      </small>
                    </div>
                  </div>
                  <div className="control-grid">
                    {(availability === "suspended" || (!live.online && wakeAvailable)
                      ? ["wake"]
                      : ["pause", "resume", "dock", "stop"]
                    ).map((action) => (
                      <button
                        key={action}
                        disabled={
                          (!live.online && action !== "wake") ||
                          !live.capabilities[action]?.supported ||
                          busy
                        }
                        title={t(live.capabilities[action]?.reason || "")}
                        className={
                          action === "resume"
                            ? "primary"
                            : action === "stop"
                              ? "danger"
                              : ""
                        }
                        onClick={() => control(action)}
                      >
                        {action === "pause"
                          ? "Ⅱ"
                          : action === "resume"
                            ? "▷"
                            : action === "dock"
                              ? "⌂"
                              : action === "wake"
                                ? "↟"
                                : "□"}{" "}
                        {t(action)}
                      </button>
                    ))}
                  </div>
                  <small className="muted">
                    {t("Every command requires deliberate confirmation.")}
                  </small>
                </section>
              </div>
              <div className="stat-grid">
                {[
                  ["battery_percent", "Battery", Zap],
                  ["mowing_speed", "Mowing speed", Gauge],
                  ["left_time", "Time remaining", CalendarDays],
                  ["wifi_rssi", "Wi-Fi signal", Wifi],
                ].map(([key, label, Icon]) => {
                  const m = metric(key as string);
                  const I = Icon as typeof Wifi;
                  return (
                    <section className="panel stat" key={key as string}>
                      <I size={19} />
                      <span>{label as string}</span>
                      <strong>
                        {m?.value || "—"} <small>{m?.unit}</small>
                      </strong>
                      <small className="muted">
                        {live.online
                          ? t("Reported by the mower connection")
                          : t("Awaiting live connection")}
                      </small>
                    </section>
                  );
                })}
              </div>
              <div className="two-columns">
                <section className="panel">
                  <div className="section-title">
                    <h2>{t("Field telemetry")}</h2>
                    <span className="pill">{t("Live")}</span>
                  </div>
                  {live.metrics.length ? (
                    <div className="telemetry-list">
                      {live.metrics.map((m) => (
                        <div key={m.id}>
                          <span>{m.name}</span>
                          <strong>
                            {m.value} {m.unit}
                          </strong>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <Empty
                      title={t("Waiting for your first connection")}
                      text="Battery, charging, RTK, satellite quality, cutting height and job progress appear as your integration exposes them."
                    />
                  )}
                </section>
                <section className="panel">
                  <div className="section-title">
                    <h2>{t("Recent activity")}</h2>
                    <Activity size={18} />
                  </div>
                  <Events events={events} />
                  <button
                    className="text-button"
                    onClick={() => setPage("Camera")}
                  >
                    {t("Open mower camera")} <ChevronRight size={16} />
                  </button>
                </section>
              </div>
            </>
          )}
          {page === "Maps" && (
            <Maps
              maps={maps}
              snapshot={snapshot}
              selected={selected}
              setSelected={setSelected}
              live={live}
              preferences={mapPreferences}
              perform={perform}
              ask={ask}
              busy={busy}
            />
          )}
          {page === "Jobs" && (
            <JobsPage
              maps={maps}
              live={live}
              preferences={mapPreferences}
              ask={ask}
              perform={perform}
              connected={live.online}
              paused={live.state === "paused"}
            />
          )}
          {page === "Schedules" && (
            <Schedules
              ask={ask}
              perform={perform}
              connected={live.connected}
              openJobs={() => setPage("Jobs")}
            />
          )}
          {page === "Drive" && (
            <Cockpit
              key={live.device?.entity_id || "unselected"}
              live={live}
              snapshot={maps.find((m) => m.id === selected)}
              preferences={mapPreferences}
            />
          )}
          {page === "Network" && (
            <NetworkHistory
              live={live}
              maps={maps}
              preferences={mapPreferences}
            />
          )}
          {page === "Diagnostics" && <Diagnostics events={events} />}
          {page === "Settings" && (
            <SettingsPage settings={settings} maps={maps} perform={perform} />
          )}
          <footer>
            <span>
              <i /> {t("Persistent maps. Connected mowing.")}
            </span>
            <span>{t("LOCAL CONSOLE / MAMMOTION")}</span>
          </footer>
        </main>
      </div>
      {toast && (
        <div className="toast" role="status">
          <Check size={18} />
          {toast}
        </div>
      )}
      {confirm && (
        <Confirm
          title={confirm.title}
          description={confirm.description}
          onConfirm={confirm.action}
          onClose={() => setConfirm(null)}
        />
      )}
    </div>
  );
}
function Empty({ title, text }: { title: string; text: string }) {
  return (
    <div className="empty">
      <Leaf size={28} />
      <h3>{title}</h3>
      <p>{text}</p>
    </div>
  );
}
function Events({ events }: { events: any[] }) {
  return events.length ? (
    <div className="events">
      {events.slice(0, 8).map((e) => (
        <div key={e.id}>
          <i />
          <div>
            <strong>{e.message}</strong>
            <small>{new Date(e.created).toLocaleString()}</small>
          </div>
        </div>
      ))}
    </div>
  ) : (
    <Empty
      title={t("A fresh start")}
      text="Map captures, mower commands and connection events will appear here."
    />
  );
}
type Shared = {
  perform: (a: () => Promise<unknown>, message?: string) => Promise<void>;
  ask: (
    title: string,
    description: React.ReactNode,
    a: () => Promise<unknown>,
  ) => void;
};
function Maps({
  maps,
  snapshot,
  selected,
  setSelected,
  live,
  preferences,
  perform,
  ask,
  busy,
}: {
  maps: Snapshot[];
  snapshot: Snapshot | undefined;
  selected: string;
  setSelected: (s: string) => void;
  live: Live;
  preferences: any;
  busy: boolean;
} & Shared) {
  const [name, setName] = useState(""),
    [notes, setNotes] = useState(""),
    [compare, setCompare] = useState(""),
    [setId, setSetId] = useState("");
  useEffect(() => {
    setName(snapshot?.name || "");
    setNotes(snapshot?.notes || "");
  }, [snapshot?.id, snapshot?.name, snapshot?.notes]);
  return (
    <>
      <div className="toolbar">
        <div>
          <span className="pill">
            {maps.length} {t("immutable snapshots")}
          </span>
          <span className="muted">{t("No automatic deletion")}</span>
        </div>
        <div>
          <label className="button">
            {t("Import map")}
            <input
              aria-label={t("Import map")}
              type="file"
              accept=".zip,.json,.geojson"
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) {
                  const body = new FormData();
                  body.append("file", file);
                  void perform(
                    () => api("/maps/import", "POST", body),
                    "Map imported",
                  );
                }
                e.target.value = "";
              }}
            />
          </label>
          <button
            className="primary"
            disabled={!live.connected || busy}
            onClick={() =>
              ask(
                "Capture complete map?",
                <p>
                  {t("Capture all available static geometry for")}{" "}
                  {live.device?.name}
                  {t(
                    ". Partial maps are rejected; previous snapshots remain unchanged.",
                  )}
                </p>,
                async () => {
                  const captured = await api<{ id: string }>("/maps/capture", "POST", {
                    name: name || "Property map",
                    set_id: setId || null,
                  });
                  setSelected(captured.id);
                },
              )
            }
          >
            <Plus size={17} />
            {t("Capture map")}
          </button>
        </div>
      </div>
      <div className="maps-grid">
        <MapView
          snapshot={snapshot}
          live={live}
          preferences={preferences}
          compare={compare}
        />
        <section className="panel map-details">
          <span className="eyebrow">{t("ARCHIVE EXPLORER")}</span>
          <h2>{t("Snapshot details")}</h2>
          <label>
            {t("Selected snapshot")}
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              <option value="">{t("Choose a snapshot")}</option>
              {maps.map((m) => (
                <option key={m.id} value={m.id}>
                  {mapLabel(m)}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("Capture into map set")}
            <select value={setId} onChange={(e) => setSetId(e.target.value)}>
              <option value="">{t("Create a new map set")}</option>
              {maps
                .filter(
                  (m, i) => maps.findIndex((x) => x.set_id === m.set_id) === i,
                )
                .map((m) => (
                  <option key={m.set_id} value={m.set_id}>
                    {m.name}
                  </option>
                ))}
            </select>
          </label>
          <p className="muted">
            {t(
              "Choose an existing set when capturing a changed version of that property.",
            )}
          </p>
          {snapshot ? (
            <>
              <label>
                {t("Name")}
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  maxLength={200}
                />
              </label>
              <label>
                {t("Notes")}
                <textarea
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  maxLength={4000}
                />
              </label>
              <button
                onClick={() =>
                  void perform(() =>
                    api(`/maps/${selected}`, "PATCH", { name, notes }),
                  )
                }
              >
                {t("Save details")}
              </button>
              <dl>
                <dt>{t("Captured")}</dt>
                <dd>{new Date(snapshot.created).toLocaleString()}</dd>
                <dt>{t("Completeness")}</dt>
                <dd>{snapshot.manifest.completeness}</dd>
                <dt>{t("Source")}</dt>
                <dd>{snapshot.manifest.source}</dd>
                <dt>{t("Fingerprint / SHA-256")}</dt>
                <dd className="mono">{snapshot.fingerprint}</dd>
              </dl>
              <label>
                {t("Compare another snapshot")}
                <select
                  value={compare}
                  onChange={(e) => setCompare(e.target.value)}
                >
                  <option value="">{t("No comparison")}</option>
                  {maps
                    .filter((m) => m.id !== selected)
                    .map((m) => (
                      <option key={m.id} value={m.id}>
                        {mapLabel(m)}
                      </option>
                    ))}
                </select>
              </label>
              <div className="actions">
                <a className="button" href={`/api/maps/${selected}/export`}>
                  <Download size={16} /> {t("Export")}
                </a>
                <button
                  className="danger"
                  onClick={() =>
                    ask(
                      "Delete this snapshot permanently?",
                      <p>
                        {t("Delete")} <strong>{mapLabel(snapshot)}</strong>
                        {t("? Export a backup first if you may need it again.")}
                      </p>,
                      () => api(`/maps/${selected}?confirmed=true`, "DELETE"),
                    )
                  }
                >
                  <Trash2 size={16} />
                  {t("Delete")}
                </button>
              </div>
            </>
          ) : (
            <Empty
              title={t("Build your map library")}
              text="Import a complete exported map or capture from your connected mower."
            />
          )}
        </section>
      </div>
      <div className="section-title library-title">
        <h2>{t("Map Library")}</h2>
        <span className="muted">
          {t("Available across every cloud-map switch")}
        </span>
      </div>
      <div className="library">
        {maps.map((m) => (
          <button
            className={`map-card ${selected === m.id ? "selected" : ""}`}
            key={m.id}
            onClick={() => setSelected(m.id)}
          >
            <img
              src={`/api/maps/${m.id}/preview`}
              alt={`Preview of ${m.name}`}
            />
            <div>
              <strong>{m.name}</strong>
              <span>
                v{m.version} · {m.manifest.zones.length} {t("zones")}
              </span>
              <small>{new Date(m.created).toLocaleDateString()}</small>
            </div>
          </button>
        ))}
      </div>
    </>
  );
}
function JobsPage({
  maps,
  live,
  preferences,
  ask,
  perform,
  connected,
  paused,
}: Shared & {
  connected: boolean;
  paused: boolean;
  maps: Snapshot[];
  live: Live;
  preferences: any;
}) {
  const [data, setData] = useState<any>({
      capability: { fields: {} },
      zones: [],
      presets: [],
      history: [],
    }),
    [fields, setFields] = useState<Record<string, any>>({ areas: [] }),
    [name, setName] = useState("My mowing preset"),
    [presetId, setPresetId] = useState(""),
    [savedSelection, setSavedSelection] = useState(""),
    [taskMap, setTaskMap] = useState(""),
    [editingZone, setEditingZone] = useState(""),
    [historyId, setHistoryId] = useState(""),
    [error, setError] = useState("");
  const load = useCallback(
    () =>
      api("/jobs")
        .then(setData)
        .catch((e) => setError(e.message)),
    [],
  );
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 10000);
    return () => clearInterval(timer);
  }, [load]);
  const zoneMatches = (live: any, archived: any, archivedZones = [archived]) => {
    const liveName = String(live?.name || "").trim();
    const archivedName = String(archived?.name || "").trim();
    const genericName = /^(zone|area)\s/i.test(liveName);
    return (
      String(live?.hash) === String(archived?.source_id) ||
      Boolean(
        live?.geometry_fingerprint &&
          archived?.geometry_fingerprint &&
          live.geometry_fingerprint === archived.geometry_fingerprint,
      ) ||
      Boolean(
        liveName &&
          !genericName &&
          archivedZones.filter(
            (zone: any) =>
              String(zone?.name || "").localeCompare(archivedName, undefined, {
                sensitivity: "accent",
              }) === 0,
          ).length === 1 &&
          liveName.localeCompare(archivedName, undefined, {
            sensitivity: "accent",
          }) === 0,
      )
    );
  };
  const matchingZones = (m: Snapshot) =>
    m.manifest.zones.filter((z) =>
      data.zones.some((live: any) => zoneMatches(live, z, m.manifest.zones)),
    ).length;
  const selectedMap = maps.find((m) => m.id === taskMap);
  const mapZoneFor = (live: any) =>
    selectedMap?.manifest.zones.find((zone) =>
      zoneMatches(live, zone, selectedMap.manifest.zones),
    );
  const profileMatchesZone = (profile: any, zone: any) => {
    const profileName = String(profile?.zone_name || "").trim();
    const zoneName = String(zone?.name || "").trim();
    return (
      String(profile?.zone_hash) === String(zone?.hash) ||
      Boolean(
        profile?.geometry_fingerprint &&
          zone?.geometry_fingerprint &&
          profile.geometry_fingerprint === zone.geometry_fingerprint,
      ) ||
      Boolean(
        profileName &&
          !/^(zone|area)\s/i.test(zoneName) &&
          data.zones.filter(
            (candidate: any) =>
              String(candidate?.name || "").localeCompare(zoneName, undefined, {
                sensitivity: "accent",
              }) === 0,
          ).length === 1 &&
          profileName.localeCompare(zoneName, undefined, {
            sensitivity: "accent",
          }) === 0,
      )
    );
  };
  const profileFor = (zone: any) =>
    data.zone_profiles?.find(
      (profile: any) =>
        profile.map_id === taskMap && profileMatchesZone(profile, zone),
    );
  const visibleZones = taskMap
    ? (selectedMap?.manifest.zones || []).map((area) => {
        const current = data.zones.find((z: any) =>
          zoneMatches(z, area, selectedMap?.manifest.zones || []),
        );
        return {
          hash: current?.hash || area.source_id,
          map_hash: area.source_id,
          geometry_fingerprint: area.geometry_fingerprint,
          name: current?.name || area.name,
          entity_id: current?.entity_id || "",
          available: Boolean(current),
        };
      })
    : data.zones.map((z: any) => ({ ...z, available: true }));
  const profileZone =
    editingZone ||
    (fields.areas?.length === 1
      ? data.zones.find((z: any) => z.entity_id === fields.areas[0])?.hash
      : "");
  const profileFingerprint = data.zones.find(
    (z: any) => String(z.hash) === String(profileZone),
  )?.geometry_fingerprint;
  const selectedZoneProfiles = (fields.areas || [])
    .map((entityId: string) => {
      const zone = data.zones.find((item: any) => item.entity_id === entityId);
      const profile = profileFor(zone);
      const mapZone = mapZoneFor(zone);
      return zone && profile && mapZone ? { zone, profile, mapZone } : null;
    })
    .filter(Boolean) as { zone: any; profile: any; mapZone: any }[];
  const sequenceReady =
    Boolean(taskMap) &&
    fields.areas?.length > 1 &&
    selectedZoneProfiles.length === fields.areas.length;
  const activeSequence = data.zone_sequences?.find((sequence: any) =>
    ["running", "starting"].includes(sequence.status),
  );
  const zoneDirections = Object.fromEntries(
    selectedZoneProfiles.map(({ zone, profile, mapZone }) => [
      String(mapZone.source_id),
      {
        name: zone.name,
        angle: Number(profile.fields.toward || 0),
        mode: Number(profile.fields.toward_mode || 0),
        cross: Number(profile.fields.channel_mode) === 1,
        includedAngle: Number(profile.fields.toward_included_angle ?? 90),
        perimeter: Number(profile.fields.channel_mode) === 3,
      },
    ]),
  );
  const editZone = (zone: any) => {
    const profile = profileFor(zone);
    setEditingZone(zone.hash);
    setFields({
      ...(profile?.fields || {}),
      areas: zone.entity_id ? [zone.entity_id] : [],
    });
    setName(zone.name);
    setPresetId("");
    setSavedSelection("");
  };
  const saveZoneProfile = async () => {
    const savedFields = Object.fromEntries(
      Object.entries(fields).filter(
        ([key]) =>
          !["areas", "modify", "start_progress", "plan_only"].includes(key),
      ),
    );
    await api("/jobs/zone-profile", "PUT", {
      map_id: taskMap,
      zone_hash: profileZone,
      zone_fingerprint: profileFingerprint || "",
      fields: savedFields,
    });
    await load();
  };
  const save = async () => {
    await api(
      `/jobs/presets${presetId ? "/" + presetId : ""}`,
      presetId ? "PUT" : "POST",
      { name, fields, map_id: taskMap },
    );
    await load();
  };
  const loadSavedSelection = (selection: string) => {
    setSavedSelection(selection);
    const [kind, id] = selection.split(":", 2);
    if (kind === "preset") {
      const preset = data.presets.find((item: any) => item.id === id);
      if (!preset) return;
      setTaskMap(preset.map_id || "");
      setEditingZone("");
      setFields(preset.fields);
      setName(preset.name);
      setPresetId(preset.id);
      return;
    }
    const profile = data.zone_profiles?.find((item: any) => item.id === id);
    if (!profile) return;
    const zone = data.zones.find((item: any) => profileMatchesZone(profile, item));
    setTaskMap(profile.map_id);
    setEditingZone(String(zone?.hash || profile.zone_hash));
    setFields({ ...profile.fields, areas: zone ? [zone.entity_id] : [] });
    setPresetId("");
    setName(profile.zone_name);
  };
  return (
    <>
      <CapabilityNote capability={data.capability} />
      {error && <p className="error">{error}</p>}
      <div className="two-columns jobs-layout">
        <section className="panel">
          <span className="eyebrow">{t("JOB COMPOSER")}</span>
          <h2>{t("Choose your zones")}</h2>
          <div className="task-planner">
            <div className="task-zones">
              <label>
                {t("Task map")}
                <select
                  value={taskMap}
                  onChange={(e) => {
                    setTaskMap(e.target.value);
                    setEditingZone("");
                    setFields({ ...fields, areas: [] });
                    setPresetId("");
                    setSavedSelection("");
                  }}
                >
                  <option value="">
                    {t("Current map / unassigned presets")}
                  </option>
                  {[...maps]
                    .sort((a, b) => matchingZones(b) - matchingZones(a))
                    .map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name} - v{m.version} ·{" "}
                        {m.created
                          ? new Date(m.created).toLocaleString()
                          : m.id.slice(0, 8)}{" "}
                        · {matchingZones(m)}/{m.manifest.zones.length}{" "}
                        {t("current zones")}
                      </option>
                    ))}
                </select>
              </label>
              <p className="muted">
                {t(
                  "Select zones for one job, or configure and save a separate profile for each zone.",
                )}
              </p>
              {selectedMap && (
                <p role="status" className="muted">
                  {selectedMap.manifest.zones.length} {t("archived zones")} ·{" "}
                  {matchingZones(selectedMap)} {t("current zones")}.{" "}
                  {t(
                    "Archives are snapshots, not additional maps on the mower. Only matching current zone IDs can be launched.",
                  )}
                </p>
              )}
              <div className="zone-list">
                {visibleZones.map((z: any) => (
                  <div className="zone-config-row" key={z.hash || z.entity_id}>
                    <label>
                      <input
                        type="checkbox"
                        disabled={!z.available}
                        checked={Boolean(
                          z.entity_id &&
                          (fields.areas || []).includes(z.entity_id),
                        )}
                        onChange={(event) => {
                          setEditingZone("");
                          setFields({
                            ...fields,
                            areas: event.target.checked
                              ? [...(fields.areas || []), z.entity_id]
                              : (fields.areas || []).filter(
                                  (id: string) => id !== z.entity_id,
                                ),
                          });
                        }}
                      />
                      {z.name}
                      {!z.available && (
                        <small>{t("Not on the active mower map")}</small>
                      )}
                    </label>
                    <button
                      disabled={!taskMap}
                      onClick={() => editZone(z)}
                      aria-label={`${t("Configure zone")}: ${z.name}`}
                    >
                      {t("Configure zone")}
                    </button>
                  </div>
                ))}
              </div>
              {!visibleZones.length && (
                <Empty
                  title={t("No zones on this map")}
                  text={t(
                    "Select an archived map or synchronize the current mower map.",
                  )}
                />
              )}
              {taskMap &&
                visibleZones.length > 0 &&
                !visibleZones.some((z: any) => z.available) && (
                  <p role="status">
                    {t(
                      "Archived zones can be configured locally. Activate this map on the mower before launching.",
                    )}
                  </p>
                )}
              <h3>{t("Mowing order")}</h3>
              {(fields.areas || []).map((id: string, i: number) => (
                <div className="order-row" key={id}>
                  <span>
                    {i + 1}.{" "}
                    {data.zones.find((z: any) => z.entity_id === id)?.name ||
                      id}
                  </span>
                  <button
                    aria-label={t("Move zone up")}
                    disabled={i === 0}
                    onClick={() => {
                      const a = [...fields.areas];
                      [a[i - 1], a[i]] = [a[i], a[i - 1]];
                      setFields({ ...fields, areas: a });
                    }}
                  >
                    <ArrowUp size={14} />
                  </button>
                  <button
                    aria-label={t("Move zone down")}
                    disabled={i === fields.areas.length - 1}
                    onClick={() => {
                      const a = [...fields.areas];
                      [a[i + 1], a[i]] = [a[i], a[i + 1]];
                      setFields({ ...fields, areas: a });
                    }}
                  >
                    <ArrowDown size={14} />
                  </button>
                </div>
              ))}
              <p className="muted">
                {t(
                  "Order is sent as selected; execution order ultimately depends on the mower firmware.",
                )}
              </p>
            </div>
            <DirectionPreview
              onAngleChange={
                data.capability.fields?.toward &&
                data.capability.fields?.toward_mode &&
                selectedZoneProfiles.length <= 1
                  ? (angle) =>
                      setFields({
                        ...fields,
                        toward: angle,
                        toward_mode: data.capability.fields.toward_mode.selector
                          ?.number
                          ? 1
                          : "1",
                      })
                  : undefined
              }
              minimum={Number(
                data.capability.fields?.toward?.selector?.number?.min ?? -180,
              )}
              maximum={Number(
                data.capability.fields?.toward?.selector?.number?.max ?? 180,
              )}
              step={Number(
                data.capability.fields?.toward?.selector?.number?.step ?? 5,
              )}
              perimeter={Number(fields.channel_mode) === 3}
              snapshot={maps.find((m) => m.id === taskMap)}
              angle={Number(fields.toward || 0)}
              mode={Number(fields.toward_mode ?? 0)}
              cross={Number(fields.channel_mode) === 1}
              includedAngle={Number(fields.toward_included_angle ?? 90)}
              zoneHashes={
                editingZone
                  ? [
                      String(
                        visibleZones.find(
                          (zone: any) => String(zone.hash) === String(editingZone),
                        )?.map_hash || editingZone,
                      ),
                    ]
                  : (fields.areas || []).map((id: string) =>
                      String(
                        mapZoneFor(
                          data.zones.find((z: any) => z.entity_id === id),
                        )?.source_id,
                      ),
                    )
              }
              zoneDirections={editingZone ? undefined : zoneDirections}
            />
          </div>
          <p className="muted">
            {t(
              "Home Assistant turn and obstacle selectors use the options exposed by the selected mower. They are applied when you confirm launch and remain mower settings after the job. Other omitted fields use the integration defaults.",
            )}
          </p>
          {editingZone && (
            <h3>
              {t("Zone profile")}:{" "}
              {visibleZones.find((z: any) => z.hash === editingZone)?.name}
            </h3>
          )}
          <p className="muted">
            {t(
              "The mower accepts one settings set per job. A per-zone sequence runs the saved profiles as separate one-zone jobs in the selected order.",
            )}
          </p>
          <Fields
            schema={data.capability.fields || {}}
            values={fields}
            onChange={setFields}
            exclude={
              data.capability.fields?.ha_bypass_mode &&
              fields.ultra_wave === undefined
                ? [
                    "areas",
                    "ultra_wave",
                    ...(editingZone
                      ? ["modify", "start_progress", "plan_only"]
                      : []),
                  ]
                : [
                    "areas",
                    ...(editingZone
                      ? ["modify", "start_progress", "plan_only"]
                      : []),
                  ]
            }
          />
          <div className="actions">
            <button
              disabled={!taskMap || !profileZone}
              onClick={() => void perform(saveZoneProfile)}
            >
              {t("Save zone profile")}
            </button>
            <button
              className="primary"
              disabled={
                !connected ||
                !data.capability.supported ||
                !fields.areas?.length
              }
              onClick={() =>
                ask(
                  "Start mowing job?",
                  <>
                    <p>
                      {name} · {fields.areas.length} {t("zones")}
                    </p>
                    <pre>{JSON.stringify(fields, null, 2)}</pre>
                  </>,
                  async () => {
                    await api("/jobs/start", "POST", {
                      confirmed: true,
                      map_id: taskMap,
                      fields,
                    });
                    await load();
                  },
                )
              }
            >
              {t("Review & start")}
            </button>
            <button
              className="primary"
              disabled={!connected || !data.capability.supported || !sequenceReady}
              onClick={() =>
                ask(
                  "Start per-zone sequence?",
                  <>
                    <p>
                      {t(
                        "Each zone starts only after the previous job was observed active and then returned to idle or docked.",
                      )}
                    </p>
                    <ol>
                      {selectedZoneProfiles.map(({ zone, profile }) => (
                        <li key={zone.hash}>
                          <strong>{zone.name}</strong>
                          <pre>{JSON.stringify(profile.fields, null, 2)}</pre>
                        </li>
                      ))}
                    </ol>
                  </>,
                  async () => {
                    await api("/jobs/zone-sequence", "POST", {
                      confirmed: true,
                      map_id: taskMap,
                      zone_hashes: selectedZoneProfiles.map(({ zone }) =>
                        String(zone.hash),
                      ),
                    });
                    await load();
                  },
                )
              }
            >
              {t("Start per-zone sequence")}
            </button>
          </div>
          {fields.areas?.length > 1 && !sequenceReady && (
            <p role="status" className="muted">
              {t("Save a zone profile for every selected zone to enable the sequence.")}
            </p>
          )}
          {activeSequence && (
            <article className="sequence-status" role="status">
              <strong>{t("Per-zone sequence running")}</strong>
              <span>
                {Number(activeSequence.current_index) + 1}/
                {activeSequence.items.length} Â·{" "}
                {activeSequence.items[activeSequence.current_index]?.zone_name}
              </span>
              <button
                onClick={() =>
                  ask(
                    "Cancel remaining zone sequence?",
                    t(
                      "The current mower job is left unchanged. No later zone will start automatically.",
                    ),
                    async () => {
                      await api("/jobs/zone-sequence/cancel", "POST", {
                        confirmed: true,
                      });
                      await load();
                    },
                  )
                }
              >
                {t("Cancel remaining sequence")}
              </button>
            </article>
          )}
        </section>
        <section className="panel">
          <span className="eyebrow">{t("LOCAL PRESETS")}</span>
          <h2>{t("Your way to mow")}</h2>
          <p className="muted">
            {t(
              "All saved profiles are listed here, including those linked to another archived map. Loading one selects its map.",
            )}
          </p>
          {!data.presets.length && !data.zone_profiles?.length && (
            <p role="status">
              {t(
                "No local profiles saved yet. Configure a zone and save its profile, or select zones and save a preset.",
              )}
            </p>
          )}
          <label>
            {t("Saved configuration")}
            <select
              value={savedSelection}
              onChange={(event) => loadSavedSelection(event.target.value)}
              disabled={!data.presets.length && !data.zone_profiles?.length}
            >
              <option value="">{t("Choose a preset or zone profile")}</option>
              {!!data.presets.length && (
                <optgroup label={t("Job presets")}>
                  {data.presets.map((preset: any) => (
                    <option key={preset.id} value={`preset:${preset.id}`}>
                      {preset.name}
                    </option>
                  ))}
                </optgroup>
              )}
              {!!data.zone_profiles?.length && (
                <optgroup label={t("Zone profiles")}>
                  {data.zone_profiles.map((profile: any) => (
                    <option key={profile.id} value={`zone:${profile.id}`}>
                      {profile.zone_name} · {maps.find((m) => m.id === profile.map_id)?.name || t("Archived map unavailable")}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
          </label>
          {!taskMap && (
            <p className="muted">
              {t("Select a map to configure individual zones.")}
            </p>
          )}
          <label>
            {t("Preset name")}
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <div className="actions">
            <button
              disabled={!fields.areas?.length}
              onClick={() => void perform(save)}
            >
              {presetId ? t("Update preset") : t("Save preset")}
            </button>
            {presetId && (
              <button
                onClick={() => {
                  setPresetId("");
                  setSavedSelection("");
                  setName(name + " copy");
                }}
              >
                {t("Duplicate")}
              </button>
            )}
          </div>
          {presetId && (
            <div className="preset">
              <small>
                {maps.find((m) => m.id === taskMap)
                  ? mapLabel(maps.find((m) => m.id === taskMap)!)
                  : t("Current map / unassigned presets")}
              </small>
              <button
                aria-label={`Delete ${name}`}
                onClick={() =>
                  ask("Delete local preset?", name, async () => {
                    await api(`/jobs/presets/${presetId}`, "DELETE");
                    setPresetId("");
                    setSavedSelection("");
                    setSavedSelection("");
                    await load();
                  })
                }
              >
                <Trash2 size={15} />
              </button>
            </div>
          )}
        </section>
      </div>
      <section className="panel">
        <span className="eyebrow">{t("JOB HISTORY")}</span>
        <h2>{t("Pick up where you left off")}</h2>
        <p className="muted">
          {t(
            "Resume continues the mower’s current paused task. Load in planner restores a past job’s zones and explicit settings for review, editing, saving as a preset, or launching a new job. Planned, traveled and estimated mowed geometry is retained on the server.",
          )}
        </p>
        <button
          disabled={!connected || !paused || !data.resume?.supported}
          onClick={() =>
            ask(
              "Resume current paused job?",
              "The mower keeps the settings and progress of its current task. This does not select a historical job.",
              async () => {
                await api("/jobs/resume", "POST", { confirmed: true });
                await load();
              },
            )
          }
        >
          {t("Resume current paused job")}
        </button>
        {!data.history?.length && (
          <p>{t("No jobs launched from this console yet.")}</p>
        )}
        {data.history?.map((item: any) => (
          <article className="history-job" key={item.id}>
            <div className="section-title">
              <div>
                <h3>{new Date(item.started).toLocaleString()}</h3>
                <span>{item.status.replaceAll("_", " ")}</span>
              </div>
              <div className="actions">
                <button
                  disabled={!item.zones?.length}
                  onClick={() =>
                    void perform(async () => {
                      const result = await api(`/jobs/history/${item.id}/fields`);
                      setFields(result.fields);
                      setTaskMap(item.route?.snapshot_id || "");
                      setEditingZone("");
                      setPresetId("");
                      setName(
                        `From ${new Date(item.started).toLocaleDateString()}`,
                      );
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    })
                  }
                >
                  {t("Load in planner")}
                </button>
                {item.route?.available && (
                  <button
                    className={historyId === item.id ? "selected" : ""}
                    onClick={() =>
                      setHistoryId(historyId === item.id ? "" : item.id)
                    }
                  >
                    {historyId === item.id
                      ? t("Hide mowing map")
                      : t("View mowing map")}
                  </button>
                )}
              </div>
            </div>
            {item.route?.available && (
              <p className="route-summary">
                {t("Actual track")}: {item.route.traveled_points || 0}{" "}
                {t("points")} · {t("Estimated mowed area")}:{" "}
                {Number(item.route.mowed_area_m2 || 0).toLocaleString(undefined, {
                  maximumFractionDigits: 1,
                })}{" "}
                m²
              </p>
            )}
            <ol>
              {item.zones.map((zone: any) => (
                <li key={zone.entity_id}>
                  <strong>{zone.name}</strong>
                  <div className="muted">
                    {Object.entries(zone.settings)
                      .map(
                        ([key, value]) =>
                          `${key.replaceAll("_", " ")}: ${String(value)}`,
                      )
                      .join(" · ") || t("Mower defaults (not recorded)")}
                  </div>
                </li>
              ))}
            </ol>
            <small>{item.settings_note}</small>
            {historyId === item.id && (
              <div className="history-map">
                <div className="section-title">
                  <div>
                    <h3>{t("Saved mowing pattern")}</h3>
                    <span>{new Date(item.started).toLocaleString()}</span>
                  </div>
                  <span className="pill">{t("Server archive")}</span>
                </div>
                <MapView
                  key={historyId}
                  snapshot={maps.find(
                    (entry) => entry.id === item.route?.snapshot_id,
                  )}
                  live={live}
                  preferences={preferences}
                  historyId={historyId}
                />
              </div>
            )}
          </article>
        ))}
      </section>
    </>
  );
}
function Schedules({
  ask,
  perform,
  connected,
  openJobs,
}: Shared & { connected: boolean; openJobs: () => void }) {
  const [data, setData] = useState<any>({
      schedules: [],
      capabilities: {},
      history: [],
    }),
    [action, setAction] = useState("create"),
    [target, setTarget] = useState(""),
    [fields, setFields] = useState<Record<string, any>>({}),
    [error, setError] = useState("");
  const load = useCallback(
    () =>
      api("/schedules")
        .then(setData)
        .catch((e) => setError(e.message)),
    [],
  );
  useEffect(() => {
    void load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);
  const cap = data.capabilities[`schedule_${action}`];
  const run = async (a: string, t?: string, f?: object) => {
    await api(`/schedules/${a}`, "POST", {
      confirmed: true,
      target: t,
      fields: f || {},
    });
    await load();
  };
  return (
    <>
      {error && <p className="error">{error}</p>}
      <section className="panel">
        <h2>{t("Looking for saved jobs and zone profiles?")}</h2>
        <p>
          {t(
            "Dashboard presets, zone settings and job history are in Jobs. This page manages timed schedules stored on the robot.",
          )}
        </p>
        <button onClick={openJobs}>{t("Open saved jobs and profiles")}</button>
      </section>
      <div className="toolbar">
        <p className="muted">
          {t(
            "Programs live on your mower. Local history preserves what was last observed.",
          )}
        </p>
        <button
          disabled={
            !connected || !data.capabilities.schedule_refresh?.supported
          }
          onClick={() =>
            void perform(() => run("refresh"), "Schedule refresh requested")
          }
        >
          <RefreshCw size={16} />
          {t("Refresh from mower")}
        </button>
      </div>
      <div className="two-columns">
        <section className="panel">
          <h2>{t("Mower schedules")}</h2>
          {data.schedules.length ? (
            data.schedules.map((s: any) => (
              <article className="schedule-card" key={s.entity_id}>
                <CalendarDays size={22} />
                <div>
                  <h3>{s.name}</h3>
                  <p className="muted">
                    {s.details
                      ? t("Cached parameters available")
                      : t("Times and enabled state not exposed by HA entity")}
                  </p>
                  {s.details && (
                    <details>
                      <summary>{t("Last cached schedule")}</summary>
                      <pre>{JSON.stringify(s.details, null, 2)}</pre>
                    </details>
                  )}
                  <div className="actions">
                    <button
                      onClick={() => {
                        setTarget(s.entity_id);
                        setAction("edit");
                        setFields({});
                      }}
                    >
                      {t("Edit…")}
                    </button>
                    <button
                      disabled={
                        !connected || !data.capabilities.schedule_run?.supported
                      }
                      onClick={() =>
                        ask("Run this schedule now?", s.name, () =>
                          run("run", s.entity_id),
                        )
                      }
                    >
                      {t("Run now…")}
                    </button>
                  </div>
                </div>
              </article>
            ))
          ) : (
            <Empty
              title={t("Schedule list not verified")}
              text={t(
                "The provider has not exposed any schedules. This does not confirm that the mower has none. Refresh from mower and check Home Assistant synchronization.",
              )}
            />
          )}
          <details>
            <summary>
              {t("Local schedule history (")}
              {data.history.length})
            </summary>
            {data.history.map((h: any) => (
              <details key={h.id}>
                <summary>
                  {new Date(h.updated).toLocaleString()} · {h.action}
                </summary>
                <pre>{JSON.stringify(h.schedules, null, 2)}</pre>
              </details>
            ))}
          </details>
        </section>
        <section className="panel">
          <h2>{t("Manage a program")}</h2>
          <label>
            {t("Operation")}
            <select
              value={action}
              onChange={(e) => {
                setAction(e.target.value);
                setFields({});
              }}
            >
              {["create", "edit", "rename", "enable", "copy", "delete"].map(
                (a) => (
                  <option key={a} value={a}>
                    {t(a)}
                  </option>
                ),
              )}
            </select>
          </label>
          {action !== "create" && (
            <label>
              {t("Schedule")}
              <select
                value={target}
                onChange={(e) => setTarget(e.target.value)}
              >
                <option value="">{t("Choose schedule")}</option>
                {data.schedules.map((s: any) => (
                  <option key={s.entity_id} value={s.entity_id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <CapabilityNote capability={cap} />
          <Fields
            schema={cap?.fields || {}}
            values={fields}
            onChange={setFields}
            exclude={["zone_hashs"]}
          />
          {cap?.fields?.zone_hashs && (
            <fieldset>
              <legend>{t("Assigned zones")}</legend>
              <div className="zone-list">
                {(data.zones || []).map((z: any) => (
                  <label key={z.entity_id}>
                    <input
                      type="checkbox"
                      checked={(fields.zone_hashs || []).includes(
                        String(z.hash),
                      )}
                      onChange={(e) =>
                        setFields({
                          ...fields,
                          zone_hashs: e.target.checked
                            ? [...(fields.zone_hashs || []), String(z.hash)]
                            : (fields.zone_hashs || []).filter(
                                (h: string) => h !== String(z.hash),
                              ),
                        })
                      }
                    />
                    {z.name}
                  </label>
                ))}
              </div>
            </fieldset>
          )}
          <button
            className={action === "delete" ? "danger" : "primary"}
            disabled={
              !connected || !cap?.supported || (action !== "create" && !target)
            }
            onClick={() =>
              ask(
                `${t(action)} schedule?`,
                <pre>{JSON.stringify(fields, null, 2)}</pre>,
                () => run(action, target, fields),
              )
            }
          >
            {t("Review &")} {t(action)}
          </button>
          <p className="muted">
            {t(
              "Only fields explicitly set here are sent. Choose enabled/disabled explicitly: the upstream service otherwise defaults to enabled. Weekdays use Sunday = 0.",
            )}
          </p>
        </section>
      </div>
    </>
  );
}
function Diagnostics({ events }: { events: any[] }) {
  const [data, setData] = useState<any>(null),
    [error, setError] = useState("");
  useEffect(() => {
    api("/diagnostics")
      .then(setData)
      .catch((e) => setError(e.message));
  }, []);
  return (
    <>
      {error && <p className="error">{error}</p>}
      <div className="toolbar">
        <span className="muted">
          {t("Connection health and detected integration capabilities")}
        </span>
        <a
          className="button"
          href="/api/diagnostics"
          download="mammotion-diagnostics.json"
        >
          <Download size={16} />
          {t("Sanitized export")}
        </a>
      </div>
      {data ? (
        <>
          <div className="stat-grid">
            <section className="panel stat">
              <span>
                {data.provider === "mammotion"
                  ? t("Mammotion client")
                  : "Home Assistant"}
              </span>
              <strong>{data.ha_version}</strong>
            </section>
            <section className="panel stat">
              <span>{t("Connection mode")}</span>
              <strong>
                {data.provider === "mammotion"
                  ? t("Direct account")
                  : "Home Assistant"}
              </strong>
            </section>
            <section className="panel stat">
              <span>{t("Connection")}</span>
              <strong>{data.connected ? t("Connected") : t("Offline")}</strong>
            </section>
            <section className="panel stat">
              <span>{t("Reconnect delay")}</span>
              <strong>{Math.round(data.retry_seconds)} s</strong>
            </section>
          </div>
          <section className="panel">
            <h2>{t("Capability matrix")}</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("Capability")}</th>
                    <th>{t("Detected")}</th>
                    <th>{t("Source / explanation")}</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(data.capabilities).map(
                    ([k, v]: [string, any]) => (
                      <tr key={k}>
                        <td>{k.replaceAll("_", " ")}</td>
                        <td>
                          <span
                            className={`pill ${v.supported ? "green" : ""}`}
                          >
                            {v.supported ? t("Available") : t("Unavailable")}
                          </span>
                        </td>
                        <td>{v.reason}</td>
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
            </div>
            <details>
              <summary>{t("Sanitized entity and service discovery")}</summary>
              <pre>{JSON.stringify(data, null, 2)}</pre>
            </details>
          </section>
          <section className="panel">
            <h2>{t("Activity & map sync history")}</h2>
            <Events events={events} />
          </section>
        </>
      ) : (
        <p>{t("Loading diagnostics…")}</p>
      )}
    </>
  );
}
function SettingsPage({
  settings,
  maps,
  perform,
}: {
  settings: any;
  maps: Snapshot[];
  perform: Shared["perform"];
}) {
  const [form, setForm] = useState(settings.preferences);
  const calibration = settings.server.map_calibration || {};
  return (
    <div className="two-columns">
      <section className="panel">
        <span className="eyebrow">{t("CONSOLE PREFERENCES")}</span>
        <h2>{t("Make it yours")}</h2>
        <label>
          {t("Mower")}
          <select
            value={form.mower_entity || ""}
            onChange={(e) => setForm({ ...form, mower_entity: e.target.value })}
          >
            <option value="">{t("Auto-discover (single mower)")}</option>
            {settings.mowers.map((m: any) => (
              <option key={m.entity_id} value={m.entity_id}>
                {m.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("Default map")}
          <select
            value={form.default_snapshot || ""}
            onChange={(e) =>
              setForm({ ...form, default_snapshot: e.target.value })
            }
          >
            <option value="">{t("Latest capture")}</option>
            {maps.map((m) => (
              <option key={m.id} value={m.id}>
                {mapLabel(m)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("Basemap")}
          <select
            value={form.tiles === "none" ? "none" : "satellite"}
            onChange={(e) => setForm({ ...form, tiles: e.target.value })}
          >
            <option value="satellite">{t("Esri satellite")}</option>
            <option value="none">{t("Offline / no external tiles")}</option>
          </select>
        </label>
        <h3>{t("Telegram notifications")}</h3>
        <p className="muted">
          {t("Set TELEGRAM_BOT_TOKEN in server .env. Bot:")}{" "}
          {settings.server.telegram_configured
            ? t("configured")
            : t("not configured")}
          {t(". Use a dedicated bot and start a conversation with it first.")}
        </p>
        <label className="check-field">
          <input
            type="checkbox"
            checked={form.telegram_enabled || false}
            disabled={!settings.server.telegram_configured}
            onChange={(e) =>
              setForm({ ...form, telegram_enabled: e.target.checked })
            }
          />{" "}
          {t("Notify on state changes")}
        </label>
        <label>
          {t("Chat ID")}
          <input
            value={form.telegram_chat_id || ""}
            onChange={(e) =>
              setForm({ ...form, telegram_chat_id: e.target.value })
            }
          />
        </label>
        <label>
          {t("Authorized user IDs for Resume (comma separated)")}
          <input
            defaultValue={(form.telegram_user_ids || []).join(",")}
            onChange={(e) =>
              setForm({
                ...form,
                telegram_user_ids: e.target.value
                  .split(",")
                  .filter((v) => v.trim())
                  .map(Number),
              })
            }
          />
        </label>
        <label className="check-field">
          <input
            type="checkbox"
            checked={form.telegram_pauses_only ?? true}
            onChange={(e) =>
              setForm({ ...form, telegram_pauses_only: e.target.checked })
            }
          />{" "}
          {t("Only pause / error / locked transitions")}
        </label>
        <p className="muted">
          {t(
            "Notifications include the zone and an Esri position image. The server captures video on demand, reusing an active Drive frame when available. Autonomous capture:",
          )}{" "}
          {settings.server.telegram_camera_capture
            ? t("enabled")
            : t("disabled")}
          {t(". Resume requires a second confirmation and an authorized user.")}
        </p>
        {settings.server.telegram_error && (
          <p className="error">{settings.server.telegram_error}</p>
        )}
        <h3>{t("Zone names by area (+/-2 m2)")}</h3>
        <p className="muted">
          {t(
            "Used for live status and Telegram. Unknown areas keep their measured value.",
          )}
        </p>
        {(form.zone_mapping || []).map((zone: any, index: number) => (
          <div className="fields" key={index}>
            <label>
              {t("Area m2")}
              <input
                type="number"
                min="1"
                step="any"
                value={zone.area_m2}
                onChange={(e) =>
                  setForm({
                    ...form,
                    zone_mapping: form.zone_mapping.map((z: any, i: number) =>
                      i === index
                        ? { ...z, area_m2: Number(e.target.value) }
                        : z,
                    ),
                  })
                }
              />
            </label>
            <label>
              {t("Zone name")}
              <input
                value={zone.name}
                onChange={(e) =>
                  setForm({
                    ...form,
                    zone_mapping: form.zone_mapping.map((z: any, i: number) =>
                      i === index ? { ...z, name: e.target.value } : z,
                    ),
                  })
                }
              />
            </label>
            <button
              onClick={() =>
                setForm({
                  ...form,
                  zone_mapping: form.zone_mapping.filter(
                    (_: any, i: number) => i !== index,
                  ),
                })
              }
            >
              {t("Remove zone")}
            </button>
          </div>
        ))}
        <button
          onClick={() =>
            setForm({
              ...form,
              zone_mapping: [
                ...(form.zone_mapping || []),
                { area_m2: 1, name: "New zone" },
              ],
            })
          }
        >
          {t("Add zone mapping")}
        </button>
        <label className="check-field">
          <input
            type="checkbox"
            checked={form.auto_capture ?? true}
            onChange={(e) =>
              setForm({ ...form, auto_capture: e.target.checked })
            }
          />
          {t("Automatically capture complete map changes")}
        </label>
        <h3>{t("Map calibration")}</h3>
        <p className="muted">
          {t(
            "Display calibration is configured in .env and is applied consistently to maps, live position, routes, Wi-Fi samples and access points. It does not alter archived coordinates.",
          )}
        </p>
        <dl className="compact-details">
          <dt>{t("Rotation")}</dt>
          <dd>{Number(calibration.rotation_degrees || 0)}°</dd>
          <dt>{t("Offset")}</dt>
          <dd>
            E {Number(calibration.offset_east_m || 0)} m · N {Number(calibration.offset_north_m || 0)} m
          </dd>
          <dt>{t("Rotation origin")}</dt>
          <dd>
            {calibration.origin_latitude == null
              ? t("Selected map centre")
              : `${calibration.origin_latitude}, ${calibration.origin_longitude}`}
          </dd>
        </dl>
        <button
          className="primary"
          onClick={() => void perform(() => api("/settings", "PUT", form))}
        >
          {t("Save preferences")}
        </button>
      </section>
      <section className="panel">
        <span className="eyebrow">{t("SERVER CONFIGURATION")}</span>
        <label>
          {t("Active connection")}
          <select
            value={settings.server.connection_mode || "home_assistant"}
            onChange={(e) => {
              const mode = e.target.value;
              void perform(async () => {
                await api("/connection", "PUT", { mode });
                window.location.reload();
              });
            }}
          >
            <option
              value="home_assistant"
              disabled={
                !settings.server.ha_configured || !settings.server.ha_base_url
              }
            >
              Home Assistant
              {!settings.server.ha_configured || !settings.server.ha_base_url
                ? t(" (configure .env first)")
                : ""}
            </option>
            <option
              value="mammotion"
              disabled={!settings.server.mammotion_configured}
            >
              Mammotion cloud
              {!settings.server.mammotion_configured
                ? t(" (configure .env first)")
                : ""}
            </option>
          </select>
        </label>
        <p className="muted">
          {t(
            "Switching disconnects video and manual controls. The mower's current job continues. Your selection is saved; credentials stay on the server.",
          )}
        </p>

        <h2>
          {settings.server.connection_mode === "mammotion"
            ? t("Mammotion account connection")
            : t("Home Assistant connection")}
        </h2>
        <dl>
          <dt>{t("Connection mode")}</dt>
          <dd>{settings.server.connection_mode || "home_assistant"}</dd>
          <dt>{t("Mammotion account")}</dt>
          <dd>
            {settings.server.mammotion_configured
              ? t("Credentials configured server-side")
              : t("Not configured")}
          </dd>
          <dt>Bluetooth</dt>
          <dd>
            {settings.server.bluetooth_enabled
              ? t("Enabled on the server")
              : t("Disabled")}
          </dd>
          <dt>{t("Base URL")}</dt>
          <dd>{settings.server.ha_base_url || t("Not configured")}</dd>
          <dt>{t("Access token")}</dt>
          <dd>
            {settings.server.ha_configured
              ? t("Configured server-side")
              : t("Missing")}
          </dd>
          <dt>{t("Read-only map cache")}</dt>
          <dd>
            {settings.server.cache_enabled ? t("Enabled") : t("Not mounted")}
          </dd>
          <dt>{t("Manual movement flag")}</dt>
          <dd>
            {settings.server.allow_manual_movement
              ? t("Enabled")
              : t("Disabled")}
          </dd>
        </dl>
        <p>
          {t("Configure")} <code>CONNECTION_MODE</code> {t("in the server’s")}{" "}
          <code>.env</code>:<code>home_assistant</code> {t("uses")}{" "}
          <code>HA_BASE_URL</code> {t("and")} <code>HA_TOKEN</code>;
          <code>mammotion</code> {t("uses")} <code>MAMMOTION_USERNAME</code>{" "}
          {t("and")} <code>MAMMOTION_PASSWORD</code>
          {t(
            ". Restart after changes. Account credentials never reach this browser.",
          )}
        </p>
        <p className="muted">
          {t("Optional: mount your Home Assistant configuration read-only at")}{" "}
          <code>/ha-config</code>
          {t(". See the deployment guide for backup and update instructions.")}
        </p>
        <div className="notice">
          <Shield size={18} />{" "}
          {t(
            "Intended for your trusted LAN. Use an authenticated reverse proxy before exposing it beyond that network.",
          )}
        </div>
      </section>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
