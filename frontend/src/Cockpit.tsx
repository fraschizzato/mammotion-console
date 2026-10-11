import { t } from "./i18n";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Live, type Snapshot } from "./api";
import { NetworkSignal } from "./Network";
import { Camera } from "./Camera";
import { MapView } from "./MapView";

export function Cockpit({
  live,
  snapshot,
  preferences,
}: {
  live: Live;
  snapshot?: Snapshot;
  preferences: any;
}) {
  const cap = live.capabilities.movement;
  const [transport, setTransport] = useState("wifi");
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const canMove =
    armed &&
    live.online &&
    !!cap?.supported &&
    !!cap.transports?.includes(transport);
  const stop = useCallback(() => {
    if (!inFlight.current) return;
    void api("/movement/stop", "POST", {}).catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    const blur = () => {
      setArmed(false);
      stop();
    };
    window.addEventListener("blur", blur);
    document.addEventListener("visibilitychange", blur);
    return () => {
      stop();
      window.removeEventListener("blur", blur);
      document.removeEventListener("visibilitychange", blur);
    };
  }, [stop]);
  const nudge = async (direction: string) => {
    if (!canMove || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      await api(`/movement/${direction}`, "POST", { transport });
    } catch (e) {
      setError((e as Error).message);
      setArmed(false);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  const arrow = (direction: string, symbol: string) => (
    <button
      aria-label={`${direction} nudge`}
      disabled={!canMove || busy}
      onPointerDown={(event) => {
        event.currentTarget.setPointerCapture(event.pointerId);
        void nudge(direction);
      }}
      onPointerUp={stop}
      onPointerCancel={stop}
      onLostPointerCapture={stop}
      onKeyDown={(event) => {
        if ((event.key === " " || event.key === "Enter") && !event.repeat) {
          event.preventDefault();
          void nudge(direction);
        }
      }}
      onKeyUp={stop}
    >
      {symbol}
    </button>
  );
  return (
    <div className="cockpit">
      <div className="cockpit-view">
        <NetworkSignal live={live} />
        <Camera
          capability={live.capabilities.camera}
          overlay={
            <aside className="cockpit-radar" aria-label={t("Mower minimap")}>
              <span className="radar-north">N</span>
              <MapView
                snapshot={snapshot}
                live={live}
                preferences={{ ...preferences, tiles: "none" }}
                compact
              />
              <span className="radar-status">
                {live.online ? t("LIVE POSITION") : t("POSITION OFFLINE")} ·{" "}
                {snapshot ? t("ARCHIVED BASE") : t("NO BASE MAP")}
              </span>
            </aside>
          }
        />
      </div>
      <section className="panel movement">
        <span className="eyebrow">{t("DRIVE CONSOLE")}</span>
        <h2>{t("Short manual nudges")}</h2>
        <label>
          {t("Connection")}
          <select
            value={transport}
            disabled={busy}
            onChange={(e) => {
              setArmed(false);
              setTransport(e.target.value);
            }}
          >
            <option value="wifi">Wi-Fi / cloud</option>
            <option value="bluetooth">Bluetooth</option>
          </select>
        </label>
        <p className="muted">
          {cap?.reason || t("Connect a mower to discover movement support.")}
        </p>
        {transport === "bluetooth" && cap?.bluetooth_error && (
          <p className="notice">{cap.bluetooth_error}</p>
        )}
        <label>
          <input
            type="checkbox"
            checked={armed}
            disabled={!cap?.supported || !live.online || busy}
            onChange={(e) => setArmed(e.target.checked)}
          />{" "}
          {t("Enable nudges for this session")}
        </label>
        <div className="dpad">
          {arrow("forward", "↑")}
          <div>
            {arrow("left", "←")}
            <button
              className="danger"
              aria-label={t("Stop manual movement")}
              onClick={stop}
              disabled={!busy}
            >
              ■
            </button>
            {arrow("right", "→")}
          </div>
          {arrow("backward", "↓")}
        </div>
        <p>
          {t(
            "Each press sends one low-speed nudge, followed by a zero-speed command after 250 ms or on release. Holding does not repeat.",
          )}
        </p>
        <p className="muted">
          {t(
            "Network latency can delay movement and stop. Keep the mower in sight and its physical stop accessible. The minimap may show a different archived map.",
          )}
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </section>
    </div>
  );
}
