import { t } from "./i18n";
import { useEffect, useId, useState } from "react";
import type { Snapshot } from "./api";
import { getMapGeometry } from "./MapView";

export function DirectionPreview({
  snapshot,
  angle,
  mode,
  cross,
  includedAngle,
  zoneHashes,
  perimeter,
  onAngleChange,
  minimum,
  maximum,
  step,
  zoneDirections,
}: {
  snapshot?: Snapshot;
  angle: number;
  mode: number;
  cross: boolean;
  includedAngle: number;
  zoneHashes: string[];
  perimeter: boolean;
  onAngleChange?: (angle: number) => void;
  minimum: number;
  maximum: number;
  step: number;
  zoneDirections?: Record<
    string,
    {
      name: string;
      angle: number;
      mode: number;
      cross: boolean;
      includedAngle: number;
      perimeter: boolean;
    }
  >;
}) {
  const [geo, setGeo] = useState<any>(null);
  const id = useId().replaceAll(":", "");
  const snapshotId = snapshot?.id;
  useEffect(() => {
    let active = true;
    setGeo(null);
    if (snapshotId)
      void getMapGeometry(snapshotId)
        .then((g) => {
          if (active) setGeo(g);
        })
        .catch(() => {});
    return () => {
      active = false;
    };
  }, [snapshotId]);
  if (!snapshot || !geo)
    return (
      <p className="muted">
        {t("Select an archived task map to preview cutting direction.")}
      </p>
    );
  const [west, south, east, north] = snapshot.manifest.bounds;
  const cosine = Math.cos((((south + north) / 2) * Math.PI) / 180);
  const scale = 280 / Math.max((east - west) * cosine, north - south, 0.000001);
  const point = ([x, y]: number[]) =>
    `${10 + (x - west) * cosine * scale},${10 + (north - y) * scale}`;
  const shapes = geo.features
    .filter(
      (f: any) =>
        f.properties?.kind === "zone" &&
        (!zoneHashes.length ||
          zoneHashes.includes(String(f.properties.source_id))),
    )
    .map((f: any) => {
      const polygons =
        f.geometry.type === "MultiPolygon"
          ? f.geometry.coordinates
          : f.geometry.type === "Polygon"
            ? [f.geometry.coordinates]
            : [];
      return {
        hash: String(f.properties.source_id),
        paths: polygons.map((rings: number[][][]) =>
          rings.map((ring) => "M" + ring.map(point).join(" L") + " Z").join(" "),
        ),
      };
    });
  const configuredDirections = Object.values(zoneDirections || {});
  const multipleDirections = configuredDirections.length > 1;
  return (
    <figure className="direction-preview">
      <div className="direction-toolbar">
        <span>{t("Cutting direction")}</span>
        <div>
          <button
            type="button"
            aria-label={t("Decrease cutting angle")}
            disabled={multipleDirections || perimeter || !onAngleChange || angle <= minimum}
            onClick={() => onAngleChange?.(Math.max(minimum, angle - step))}
          >
            −
          </button>
          <output aria-label={t("Cutting angle")}>{angle}°</output>
          <button
            type="button"
            aria-label={t("Increase cutting angle")}
            disabled={multipleDirections || perimeter || !onAngleChange || angle >= maximum}
            onClick={() => onAngleChange?.(Math.min(maximum, angle + step))}
          >
            +
          </button>
        </div>
      </div>
      <div className="direction-control">
        <svg
          className="direction-compass"
          viewBox="0 0 100 100"
          role="img"
          aria-label={`${t("Cutting direction")} ${angle} degrees`}
        >
          <circle cx="50" cy="50" r="43" />
          <text x="50" y="14">N</text>
          <text x="88" y="54">E</text>
          <text x="50" y="94">S</text>
          <text x="12" y="54">W</text>
          <g transform={`rotate(${angle} 50 50)`}>
            <path className="compass-needle" d="M50 18 L58 52 L50 47 L42 52 Z" />
            <line x1="50" y1="48" x2="50" y2="78" />
          </g>
          <circle className="compass-hub" cx="50" cy="50" r="4" />
        </svg>
        <label>
          <span>
            {t("Cutting angle")}: <strong>{angle}°</strong>
          </span>
          <input
            aria-label={t("Cutting angle slider")}
            type="range"
            min={minimum}
            max={maximum}
            step={step}
            value={Math.min(maximum, Math.max(minimum, angle))}
            disabled={multipleDirections || perimeter || !onAngleChange}
            onChange={(event) => onAngleChange?.(Number(event.target.value))}
          />
          <small>
            {minimum}° – {maximum}° · {t("step")} {step}°
          </small>
        </label>
      </div>
      <svg
        viewBox="0 0 300 300"
        role="img"
        aria-label={`Indicative cutting direction ${angle} degrees`}
      >
        <defs>
          {shapes.map((shape: any, index: number) => {
            const direction = zoneDirections?.[shape.hash];
            const shapeAngle = direction?.angle ?? angle;
            const shapeIncluded = direction?.includedAngle ?? includedAngle;
            return (
              <g key={`patterns-${shape.hash}-${index}`}>
                <pattern
                  id={`${id}-${index}`}
                  width="12"
                  height="12"
                  patternUnits="userSpaceOnUse"
                  patternTransform={`rotate(${shapeAngle})`}
                >
                  <path
                    d="M6 0 V12 M3 5 L6 1 L9 5"
                    stroke="#b4e88d"
                    fill="none"
                    strokeWidth="1"
                  />
                </pattern>
                <pattern
                  id={`${id}-${index}-cross`}
                  width="12"
                  height="12"
                  patternUnits="userSpaceOnUse"
                  patternTransform={`rotate(${shapeAngle + shapeIncluded})`}
                >
                  <path d="M6 0 V12" stroke="#83cdd6" />
                </pattern>
              </g>
            );
          })}
        </defs>
        {shapes.map((shape: any, index: number) => {
          const direction = zoneDirections?.[shape.hash];
          const shapeCross = direction?.cross ?? cross;
          const shapePerimeter = direction?.perimeter ?? perimeter;
          return shape.paths.map((d: string, pathIndex: number) => (
            <g key={`${shape.hash}-${pathIndex}`}>
              <path
                d={d}
                fill={shapePerimeter ? "none" : `url(#${id}-${index})`}
                fillRule="evenodd"
                stroke="#b4e88d"
              />
              {shapeCross && !shapePerimeter && (
                <path
                  d={d}
                  fill={`url(#${id}-${index}-cross)`}
                  fillRule="evenodd"
                />
              )}
            </g>
          ));
        })}
        <text x="280" y="16" fill="white">
          N ↑
        </text>
      </svg>
      <figcaption>
        {multipleDirections
          ? configuredDirections
              .map((direction) => `${direction.name}: ${direction.angle}\u00b0`)
              .join(" / ")
          : perimeter
          ? t("Perimeter only: no interior cutting lanes")
          : mode === 1
            ? `${angle}° ${t("from north")}`
            : mode === 2
              ? t("Random angle: illustration only")
              : t("Optimal angle: selected by mower; illustration only")}
        {t(". Schematic preview, not a generated route.")}
      </figcaption>
    </figure>
  );
}
