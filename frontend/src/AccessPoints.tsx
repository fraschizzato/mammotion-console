import { useEffect, useState } from "react";
import { api } from "./api";
import { t } from "./i18n";

export type AccessPoint = {
  id?: string;
  name: string;
  ssid: string;
  bssid: string;
  environment: "indoor" | "outdoor";
  latitude: number;
  longitude: number;
};
export function AccessPoints({
  points,
  onChange,
  draft,
  setDraft,
}: {
  points: AccessPoint[];
  onChange: (points: AccessPoint[]) => void;
  draft: AccessPoint | null;
  setDraft: (point: AccessPoint | null) => void;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    api<AccessPoint[]>("/network/access-points")
      .then((p) => {
        if (active) onChange(p);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [onChange]);
  async function save() {
    if (!draft) return;
    setBusy(true);
    setError("");
    try {
      const { id, name, ssid, bssid, environment, latitude, longitude } = draft;
      await api(
        `/network/access-points${id ? `/${id}` : ""}`,
        id ? "PUT" : "POST",
        { name, ssid, bssid, environment, latitude, longitude },
      );
      onChange(await api("/network/access-points"));
      setDraft(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function remove(id: string) {
    setBusy(true);
    setError("");
    try {
      await api(`/network/access-points/${id}`, "DELETE");
      onChange(await api("/network/access-points"));
      if (draft?.id === id) setDraft(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel">
      <h2>{t("Access points")}</h2>
      <p>
        {t(
          "Click the network map to place an access point. Select Edit to move an existing point with another map click.",
        )}
      </p>
      <p className="muted">
        {t(
          "AP locations and BSSIDs are entered manually; proximity does not identify the connected AP.",
        )}
      </p>
      {draft && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <div className="network-filters">
            <label>
              {t("Name")}
              <input
                required
                maxLength={100}
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              />
            </label>
            <label>
              SSID
              <input
                maxLength={32}
                value={draft.ssid}
                onChange={(e) => setDraft({ ...draft, ssid: e.target.value })}
              />
            </label>
            <label>
              BSSID / MAC AP
              <input
                placeholder="AA:BB:CC:DD:EE:FF"
                pattern="([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}"
                value={draft.bssid}
                onChange={(e) => setDraft({ ...draft, bssid: e.target.value })}
              />
            </label>
            <label>
              {t("Environment")}
              <select
                value={draft.environment}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    environment: e.target.value as AccessPoint["environment"],
                  })
                }
              >
                <option value="indoor">{t("indoor")}</option>
                <option value="outdoor">{t("outdoor")}</option>
              </select>
            </label>
            <label>
              {t("Latitude")}
              <input
                type="number"
                step="any"
                min={-90}
                max={90}
                required
                value={draft.latitude}
                onChange={(e) =>
                  setDraft({ ...draft, latitude: Number(e.target.value) })
                }
              />
            </label>
            <label>
              {t("Longitude")}
              <input
                type="number"
                step="any"
                min={-180}
                max={180}
                required
                value={draft.longitude}
                onChange={(e) =>
                  setDraft({ ...draft, longitude: Number(e.target.value) })
                }
              />
            </label>
          </div>
          <button disabled={busy} type="submit">
            {t("Save access point")}
          </button>{" "}
          <button type="button" disabled={busy} onClick={() => setDraft(null)}>
            {t("Cancel")}
          </button>
        </form>
      )}
      <div className="network-filters">
        {points.map((p) => (
          <div key={p.id}>
            <strong>{p.name}</strong> · {t(p.environment)}
            <br />
            {p.ssid || "SSID —"} · {p.bssid || "BSSID —"}
            <br />
            <button disabled={busy} onClick={() => setDraft(p)}>
              {t("Edit")}
            </button>{" "}
            <button disabled={busy} onClick={() => void remove(p.id!)}>
              {t("Delete access point")}
            </button>
          </div>
        ))}
      </div>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
