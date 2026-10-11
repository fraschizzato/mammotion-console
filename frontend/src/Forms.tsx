import { t } from "./i18n";
import { useState } from "react";
import type { Capability } from "./api";

const fieldLabels: Record<string, string> = {
  modify: "Modify current job",
  speed: "Speed",
  blade_height: "Cutting height",
  channel_width: "Lane spacing",
  ultra_wave: "Obstacle avoidance",
  channel_mode: "Path pattern",
  toward: "Cutting direction (degrees)",
  toward_mode: "Direction mode",
  border_mode: "Perimeter order",
  auto_change_direction: "Auto-reverse mowing direction",
  ha_auto_change_direction: "Auto-reverse mowing direction",
  ride_boundary_distance: "Edge coverage",
  rain_tactics: "Rain response",
};
const optionLabels: Record<string, Record<string, string>> = {
  ha_turning_mode: {
    multipoint: "Svolta multipunto",
    zero_turn: "Rotazione sul posto",
  },
  ha_bypass_mode: {
    direct_touch: "Off / Direct touch",
    slow_touch: "Slow touch",
    less_touch: "Less touch",
    no_touch: "Standard / No touch",
    sensitive: "Sensitive",
  },
  ultra_wave: {
    "0": "Direct touch",
    "1": "Slow touch",
    "2": "Less touch",
    "10": "No touch",
    "11": "Sensitive",
  },
  channel_mode: {
    "0": "Zigzag",
    "1": "Chessboard",
    "2": "Adaptive zigzag",
    "3": "Perimeter only",
  },
  toward_mode: {
    "0": "Optimal angle",
    "1": "North angle",
    "2": "Random angle",
  },
};

export function Fields({
  schema,
  values,
  onChange,
  exclude = [],
}: {
  schema: Record<string, any>;
  values: Record<string, any>;
  onChange: (v: Record<string, any>) => void;
  exclude?: string[];
}) {
  return (
    <div className="fields">
      {Object.entries(schema)
        .filter(([key]) => !exclude.includes(key))
        .map(([key, def]) => {
          const selector = def.selector || {},
            value = values[key],
            title = def.name || fieldLabels[key] || key.replaceAll("_", " ");
          const update = (v: unknown) => {
            const next = { ...values };
            if (v === "") delete next[key];
            else next[key] = v;
            onChange(next);
          };
          if (key === "weeks")
            return (
              <fieldset key={key}>
                <legend>{t("Days of week")}</legend>
                <div className="weekdays">
                  {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map(
                    (day, i) => (
                      <label key={t(day)}>
                        <input
                          type="checkbox"
                          checked={(value || []).includes((i + 1) % 7)}
                          onChange={(e) =>
                            update(
                              e.target.checked
                                ? [...(value || []), (i + 1) % 7]
                                : (value || []).filter(
                                    (x: number) => x !== (i + 1) % 7,
                                  ),
                            )
                          }
                        />
                        {t(day)}
                      </label>
                    ),
                  )}
                </div>
              </fieldset>
            );
          if ("boolean" in selector)
            return (
              <label key={key}>
                {t(title)}
                <select
                  value={value === undefined ? "" : String(value)}
                  disabled={
                    key === "ride_boundary_distance" && values.mowing_laps === 0
                  }
                  onChange={(e) =>
                    update(
                      e.target.value === "" ? "" : e.target.value === "true",
                    )
                  }
                >
                  <option value="">
                    {key === "enabled"
                      ? t("Choose explicitly")
                      : t("Integration default")}
                  </option>
                  <option value="true">{t("Yes")}</option>
                  <option value="false">{t("No")}</option>
                </select>
                <small>{def.description}</small>
                {key === "ride_boundary_distance" && values.mowing_laps === 0 && (
                  <small>{t("Enable at least one perimeter lap first.")}</small>
                )}
              </label>
            );
          if ("select" in selector)
            return (
              <label key={key}>
                {t(title)}
                <select
                  value={value ?? ""}
                  onChange={(e) => update(e.target.value)}
                >
                  <option value="">{t("Integration default")}</option>
                  {(selector.select.options || []).map((o: any) => (
                    <option
                      key={typeof o === "object" ? o.value : o}
                      value={typeof o === "object" ? o.value : o}
                    >
                      {typeof o === "object"
                        ? t(o.label)
                        : t(optionLabels[key]?.[String(o)] || String(o))}
                    </option>
                  ))}
                </select>
              </label>
            );
          if ("number" in selector) {
            const number = selector.number || {};
            const minimum = Number(number.min);
            const maximum = Number(number.max);
            const step = Number(number.step) || 1;
            const bounded = Number.isFinite(minimum) && Number.isFinite(maximum);
            const sliderValue = Number(
              value ?? def.default ?? (bounded ? minimum : 0),
            );
            return (
              <label key={key} className="numeric-field">
                <span className="field-heading">
                  <span>{t(title)}</span>
                  <small>{number.unit_of_measurement}</small>
                </span>
                <span className="numeric-controls">
                  {bounded && (
                    <input
                      aria-label={`${t(title)} ${t("slider")}`}
                      type="range"
                      step={step}
                      min={minimum}
                      max={maximum}
                      value={Math.min(maximum, Math.max(minimum, sliderValue))}
                      onChange={(e) => update(Number(e.target.value))}
                    />
                  )}
                  <input
                    aria-label={t(title)}
                    type="number"
                    step={number.step || "any"}
                    min={number.min}
                    max={number.max}
                    value={value ?? ""}
                    placeholder={t(String(def.default ?? "Default"))}
                    onChange={(e) =>
                      update(e.target.value === "" ? "" : Number(e.target.value))
                    }
                  />
                </span>
                {bounded && (
                  <small className="range-limits">
                    {minimum} – {maximum} · {t("step")} {step}
                  </small>
                )}
              </label>
            );
          }
          if ("object" in selector)
            return (
              <label key={key}>
                {t(title)}
                <input
                  value={Array.isArray(value) ? value.join(", ") : ""}
                  placeholder={t("Comma-separated numbers")}
                  onChange={(e) =>
                    update(
                      e.target.value
                        ? e.target.value.split(",").map((x) => Number(x.trim()))
                        : [],
                    )
                  }
                />
              </label>
            );
          return (
            <label key={key}>
              {t(title)}
              <input
                type={
                  key.endsWith("_time")
                    ? "time"
                    : key.endsWith("_date")
                      ? "date"
                      : "text"
                }
                value={value ?? ""}
                onChange={(e) => update(e.target.value)}
                maxLength={200}
              />
            </label>
          );
        })}
    </div>
  );
}

export function Confirm({
  title,
  description,
  onConfirm,
  onClose,
}: {
  title: string;
  description: React.ReactNode;
  onConfirm: () => Promise<void>;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <div className="modal-backdrop">
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={t(title)}
      >
        <span className="eyebrow">{t("REVIEW & CONFIRM")}</span>
        <h2>{t(title)}</h2>
        <div className="review">{description}</div>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="actions">
          <button disabled={busy} onClick={onClose}>
            {t("Cancel")}
          </button>
          <button
            autoFocus
            className="primary"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await onConfirm();
                onClose();
              } catch (e) {
                setError((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? t("Sending…") : t("Confirm")}
          </button>
        </div>
      </section>
    </div>
  );
}
export function CapabilityNote({ capability }: { capability?: Capability }) {
  return !capability?.supported ? (
    <div className="notice">
      {capability?.reason ||
        t("Connect Home Assistant to discover supported controls.")}
    </div>
  ) : null;
}
