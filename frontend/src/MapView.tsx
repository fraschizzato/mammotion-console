import type { NetworkData } from "./Network";
import type { AccessPoint } from "./AccessPoints";
import { t } from "./i18n";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import type { FeatureCollection, GeoJsonObject, Position } from "geojson";
import { Crosshair, Layers, Maximize2 } from "lucide-react";
import { api, type Live, type Snapshot } from "./api";
import {
  inverseMapPosition,
  resolveMapTransformOrigin,
  transformMapFeatures,
  transformMapPosition,
  type MapCalibration,
} from "./mapTransform";
import "leaflet/dist/leaflet.css";

export const signalColors: Record<string, string> = {
  excellent: "#78d98c",
  good: "#b4e88d",
  fair: "#f0bd5a",
  weak: "#ee786f",
  unknown: "#89958d",
};

const geometryCache = new Map<string, Promise<FeatureCollection>>();
export function getMapGeometry(id: string) {
  if (!geometryCache.has(id)) {
    if (geometryCache.size > 12) geometryCache.clear();
    geometryCache.set(
      id,
      api<FeatureCollection>(`/maps/${id}/geojson`).catch((error) => {
        geometryCache.delete(id);
        throw error;
      }),
    );
  }
  return geometryCache.get(id)!;
}
const colors = ["#b4e88d", "#83cdd6", "#d8bf7c", "#c6a4df", "#e5aa91"];
export function zoneColor(id: string) {
  return colors[
    [...id].reduce((a, c) => a + c.charCodeAt(0), 0) % colors.length
  ];
}
export function MapView({
  snapshot,
  live,
  preferences,
  compare,
  compact = false,
  network,
  networkMode = false,
  accessPoints,
  draftAccessPoint,
  onMapPoint,
  historyId,
}: {
  snapshot: Snapshot | undefined;
  live: Live;
  preferences: any;
  compare?: string;
  compact?: boolean;
  network?: NetworkData;
  networkMode?: boolean;
  accessPoints?: AccessPoint[];
  draftAccessPoint?: AccessPoint | null;
  onMapPoint?: (latitude: number, longitude: number) => void;
  historyId?: string;
}) {
  const element = useRef<HTMLDivElement>(null),
    map = useRef<L.Map | null>(null),
    base = useRef<L.GeoJSON | null>(null);
  const [geo, setGeo] = useState<FeatureCollection | null>(null),
    [error, setError] = useState(""),
    [layers, setLayers] = useState(false);
  const [hidden, setHidden] = useState<string[]>([]),
    [overlay, setOverlay] = useState(true),
    [tiles, setTiles] = useState(
      preferences.tiles === "none" ? "none" : "satellite",
    );
  const [progress, setProgress] = useState(!networkMode);
  const [coverageAvailable, setCoverageAvailable] = useState(false);
  const [coverageArea, setCoverageArea] = useState<number | null>(null);
  const [routeStatus, setRouteStatus] = useState("");
  const snapshotId = snapshot?.id;
  const [snapshotMatches, setSnapshotMatches] = useState<boolean | null | undefined>(
    snapshotId ? undefined : true,
  );
  const positionOnline = live.position_online ?? live.online;
  const mapCalibration = preferences.map_calibration as MapCalibration | undefined;
  const mapOrigin = useMemo(
    () =>
      resolveMapTransformOrigin(
        mapCalibration,
        snapshot?.manifest.bounds,
        live.position
          ? { latitude: live.position.latitude, longitude: live.position.longitude }
          : null,
      ),
    [mapCalibration, snapshot?.manifest.bounds, live.position],
  );
  const displayPosition = useCallback(
    (position: { latitude: number; longitude: number }) => {
      const transformed = transformMapPosition(
        [position.longitude, position.latitude],
        mapCalibration,
        mapOrigin,
      );
      return { latitude: Number(transformed[1]), longitude: Number(transformed[0]) };
    },
    [mapCalibration, mapOrigin],
  );
  const positionMatchesMap =
    networkMode || !snapshotId || snapshotMatches === true;
  const positionVisible = Boolean(
    !historyId && live.position && positionOnline && overlay && positionMatchesMap,
  );
  const centeredOnPosition = useRef(false);
  const centeredOnNetwork = useRef(false);
  useEffect(() => {
    const m = map.current;
    if (!m || !network) return;
    const layer = L.geoJSON(transformMapFeatures(network, mapCalibration, mapOrigin), {
      pointToLayer: (f, latlng) =>
        L.circle(latlng, {
          radius: 6,
          weight: 1,
          color: signalColors[f.properties.quality] || signalColors.unknown,
          fillOpacity: 0.55,
          className: "network-heat-cell",
        }),
      onEachFeature: (f, item) => {
        const p = f.properties;
        const label = document.createElement("span");
        label.textContent = `${Number(p.mean_dbm).toFixed(1)} dBm / ${p.samples} ${t("samples")} / ${p.min_dbm} to ${p.max_dbm} dBm / ${new Date(p.last_observed * 1000).toLocaleString()}`;
        item.bindTooltip(label);
      },
    }).addTo(m);
    if (
      !snapshotId &&
      !centeredOnNetwork.current &&
      layer.getBounds().isValid()
    ) {
      m.fitBounds(layer.getBounds(), {
        padding: [35, 35],
        maxZoom: 20,
        animate: false,
      });
      centeredOnNetwork.current = true;
    }
    return () => {
      layer.remove();
    };
  }, [network, snapshotId, mapCalibration, mapOrigin]);
  useEffect(() => {
    if (
      !snapshotId &&
      !centeredOnPosition.current &&
      live.position &&
      map.current
    ) {
      const position = displayPosition(live.position);
      map.current.setView([position.latitude, position.longitude], 19, { animate: false });
      centeredOnPosition.current = true;
    }
  }, [snapshotId, live.position, displayPosition]);
  useEffect(() => {
    if (compact && positionVisible && live.position)
      {
        const position = displayPosition(live.position);
        map.current?.setView([position.latitude, position.longitude], 20, {
          animate: false,
        });
      }
  }, [compact, positionVisible, live.position, displayPosition]);
  useEffect(() => {
    if (!element.current) return;
    map.current = L.map(element.current, {
      zoomControl: false,
      zoomAnimation: false,
      fadeAnimation: false,
      markerZoomAnimation: false,
    }).setView(
      snapshot
        ? [
            (snapshot.manifest.bounds[1] + snapshot.manifest.bounds[3]) / 2,
            (snapshot.manifest.bounds[0] + snapshot.manifest.bounds[2]) / 2,
          ]
        : live.position
          ? [live.position.latitude, live.position.longitude]
          : [0, 0],
      16,
    );
    L.control.zoom({ position: "bottomright" }).addTo(map.current);
    const observer = new ResizeObserver(() => {
      map.current?.invalidateSize();
      if (!compact && base.current?.getBounds().isValid())
        map.current?.fitBounds(base.current.getBounds(), {
          padding: [35, 35],
          maxZoom: 20,
          animate: false,
        });
    });
    observer.observe(element.current);
    return () => {
      observer.disconnect();
      const previous = map.current;
      map.current = null;
      base.current = null;
      previous?.stop();
      previous?.remove();
    };
    // Initial view only; subsequent movement and geometry have their own effects.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    const m = map.current;
    if (!m || !onMapPoint) return;
    const click = (e: L.LeafletMouseEvent) => {
      const source = inverseMapPosition(
        [e.latlng.lng, e.latlng.lat],
        mapCalibration,
        mapOrigin,
      );
      onMapPoint(Number(source[1]), Number(source[0]));
    };
    m.on("click", click);
    return () => {
      m.off("click", click);
    };
  }, [onMapPoint, mapCalibration, mapOrigin]);
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const items = [
      ...(accessPoints || []).filter(
        (p) => !draftAccessPoint?.id || p.id !== draftAccessPoint.id,
      ),
      ...(draftAccessPoint ? [draftAccessPoint] : []),
    ];
    const layer = L.layerGroup(
      items.map((p, index) => {
        const draft = index === items.length - 1 && Boolean(draftAccessPoint);
        const position = displayPosition(p);
        const marker = L.circleMarker([position.latitude, position.longitude], {
          radius: draft ? 12 : 9,
          weight: 3,
          color: draft
            ? "#ffffff"
            : p.environment === "indoor"
              ? "#83cdd6"
              : "#e7bc68",
          fillOpacity: 0.9,
          className: "access-point-marker",
          bubblingMouseEvents: false,
        });
        const label = document.createElement("span");
        label.textContent = `${p.name} · ${t(p.environment)}${p.ssid ? ` · ${p.ssid}` : ""}${p.bssid ? ` · ${p.bssid}` : ""}`;
        marker.bindTooltip(label, { permanent: true, direction: "top" });
        return marker;
      }),
    ).addTo(m);
    return () => {
      layer.remove();
    };
  }, [accessPoints, draftAccessPoint, displayPosition]);
  useEffect(() => {
    let active = true;
    setGeo(null);
    setError("");
    if (snapshotId)
      getMapGeometry(snapshotId)
        .then((g) => {
          if (active) setGeo(g);
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    return () => {
      active = false;
    };
  }, [snapshotId]);
  useEffect(() => {
    const m = map.current;
    if (!m || tiles === "none") return;
    const tile = L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      {
        maxZoom: 21,
        maxNativeZoom: 19,
        attribution: "Tiles Esri, Maxar, Earthstar Geographics",
      },
    ).addTo(m);
    return () => {
      tile.remove();
    };
  }, [tiles]);
  useEffect(() => {
    const m = map.current;
    if (!m || !geo) return;
    const copy = transformMapFeatures(geo, mapCalibration, mapOrigin);
    const layer = L.geoJSON(copy, {
      filter: (f) =>
        f.properties?.kind === "zone" ||
        !hidden.includes(f.properties?.source_id || f.properties?.kind),
      style: (f) => ({
        color:
          f?.properties?.kind === "zone"
            ? zoneColor(f.properties.source_id)
            : "#e9a17e",
        weight: 2,
        fillOpacity: 0.2,
        dashArray: f?.properties?.kind === "zone" ? undefined : "5 5",
      }),
      pointToLayer: (_f, p) =>
        L.circleMarker(p, { radius: 7, color: "#e7e9b0", fillOpacity: 1 }),
      onEachFeature: (f, l) => {
        const label = document.createElement("span");
        label.textContent = f.properties?.name || "Map feature";
        l.bindTooltip(label, {
          permanent: f.properties?.kind === "zone",
          direction: "center",
          className: "zone-label",
        });
      },
    }).addTo(m);
    base.current = layer;
    return () => {
      layer.remove();
    };
  }, [geo, hidden, mapCalibration, mapOrigin]);
  useEffect(() => {
    if (!compact && geo && map.current && base.current?.getBounds().isValid())
      map.current.fitBounds(base.current.getBounds(), {
        padding: [45, 45],
        maxZoom: 20,
        animate: false,
      });
    // Fit once per snapshot; live position updates are handled separately.
  }, [geo, compact]);
  useEffect(() => {
    const m = map.current;
    if (
      !m ||
      !overlay ||
      !live.position ||
      !positionOnline ||
      (!networkMode && snapshotId && snapshotMatches !== true)
    )
      return;
    const p = live.position;
    const position = displayPosition(p);
    const marker = L.marker([position.latitude, position.longitude], {
      zIndexOffset: 1000,
      icon: L.divIcon({
        className: "mower-marker",
        html: `<span style="transform:rotate(${Number(p.direction ?? p.heading ?? p.rotation ?? 0) + Number(mapCalibration?.rotation_degrees || 0)}deg)">▲</span>`,
        iconSize: [32, 32],
        iconAnchor: [16, 16],
      }),
    }).addTo(m);
    marker.bindTooltip(t("Robot"), {
      permanent: true,
      direction: "right",
      offset: [18, 0],
      className: "mower-label",
    });
    return () => {
      marker.remove();
    };
  }, [
    live.position,
    positionOnline,
    overlay,
    networkMode,
    snapshotId,
    snapshotMatches,
    mapCalibration,
    mapOrigin,
    displayPosition,
  ]);
  useEffect(() => {
    let cancelled = false;
    const m = map.current;
    let layer: L.GeoJSON | undefined;
    if (compare && m)
      getMapGeometry(compare)
        .then((g) => {
          if (!cancelled)
            layer = L.geoJSON(transformMapFeatures(g, mapCalibration, mapOrigin), {
              style: {
                color: "#f6b365",
                fillOpacity: 0.05,
                dashArray: "8 7",
                weight: 3,
              },
            }).addTo(m);
        })
        .catch((e) => setError(e.message));
    return () => {
      cancelled = true;
      layer?.remove();
    };
  }, [compare, mapCalibration, mapOrigin]);
  useEffect(() => {
    setSnapshotMatches(snapshotId ? undefined : true);
    if (!historyId && !(live.connected || live.mqtt?.connected)) return;
    let cancelled = false;
    let drawn: L.GeoJSON[] = [];
    let loading = false;
    const query = new URLSearchParams();
    if (!historyId && snapshotId) query.set("snapshot_id", snapshotId);
    if (!historyId && !progress) query.set("include_route", "false");
    const endpoint = historyId
      ? `/jobs/history/${historyId}/overlay`
      : `/overlay${query.size ? `?${query.toString()}` : ""}`;
    const refresh = () => {
      if (loading) return;
      loading = true;
      return api<
        Record<string, GeoJsonObject> & {
          status?: {
            stale: boolean;
            route_current?: boolean | null;
            snapshot_matches?: boolean | null;
            persisted?: boolean;
          };
        }
      >(endpoint)
        .then((data) => {
          if (cancelled || !map.current) return;
          const match =
            data.status && "snapshot_matches" in data.status
              ? data.status.snapshot_matches
              : true;
          setSnapshotMatches(match);
          setRouteStatus(
            historyId
              ? t("Saved job route")
              : data.status?.persisted && data.status?.route_current === false
                ? t("Showing saved job data while the live route synchronizes")
              : snapshotId && match === false
              ? t("Live overlay hidden · selected archive is not the active map")
              : snapshotId && match == null
                ? t("Live overlay hidden · current map could not be verified")
                : data.status?.stale
              ? t("Last received route · telemetry is stale")
              : data.status?.route_current === false
                ? t("Synchronizing current route")
                : "",
          );
          drawn.forEach((l) => l.remove());
          drawn = [];
          if (snapshotId && match !== true) {
            setCoverageAvailable(false);
            setCoverageArea(null);
            return;
          }
          if (!progress) {
            setCoverageAvailable(false);
            setCoverageArea(null);
            return;
          }
          const mowed = data.mowed as FeatureCollection | undefined;
          setCoverageAvailable(Boolean(mowed?.features?.length));
          const measuredArea = (mowed?.features || []).reduce((total, feature) => {
            const area = Number(feature.properties?.area_m2);
            return Number.isFinite(area) && area > 0 ? total + area : total;
          }, 0);
          setCoverageArea(measuredArea > 0 ? measuredArea : null);
          drawn = ["path", "progress", "traveled", "mowed"]
            .map((kind) => [kind, data[kind]] as const)
            .filter(([, g]) => g?.type)
            .map(([kind, g]) =>
              L.geoJSON(transformMapFeatures(g as FeatureCollection, mapCalibration, mapOrigin), {
                style:
                  kind === "mowed"
                    ? {
                        className: "mowed-area",
                        color: "#64df87",
                        fillColor: "#64df87",
                        weight: 1,
                        fillOpacity: 0.45,
                      }
                    : {
                        color:
                          kind === "progress"
                            ? "#ffe082"
                            : kind === "traveled"
                              ? "#f0a95b"
                              : "#c2d9dc",
                        weight: 2,
                        fillOpacity: 0,
                        dashArray: kind === "progress" ? "5 5" : undefined,
                      },
              }).addTo(map.current!),
            );
          if (historyId && drawn.length && !base.current?.getBounds().isValid()) {
            const bounds = L.featureGroup(drawn).getBounds();
            if (bounds.isValid())
              map.current.fitBounds(bounds, {
                padding: [35, 35],
                maxZoom: 20,
                animate: false,
              });
          }
        })
        .catch((e) => setError(e.message))
        .finally(() => {
          loading = false;
        });
    };
    void refresh();
    const timer = setInterval(refresh, 10000);
    return () => {
      cancelled = true;
      clearInterval(timer);
      drawn.forEach((l) => l.remove());
    };
  }, [
    progress,
    live.connected,
    live.mqtt?.connected,
    live.device?.entity_id,
    snapshotId,
    historyId,
    mapCalibration,
    mapOrigin,
  ]);
  const fit = () => {
    if (base.current?.getBounds().isValid())
      map.current?.fitBounds(base.current.getBounds(), {
        padding: [40, 40],
        animate: false,
      });
  };
  return (
    <div className="map-shell">
      <div
        ref={element}
        className="map-canvas"
        aria-label={t("Property map")}
      />
      {!snapshot && !compact && !networkMode && (
        <div className="map-empty">
          <span className="eyebrow">{t("YOUR PROPERTY, PRESERVED")}</span>
          <h2>{t("A permanent home for every map.")}</h2>
          <p>
            {t(
              "Capture a complete map from Home Assistant or import an archived snapshot to explore your property.",
            )}
          </p>
          <span className="pill">
            {t("Local archive · independent of cloud restores")}
          </span>
        </div>
      )}
      <div className="map-top">
        <span className="map-tag">
          <i />
          {snapshot
            ? `${snapshot.name} · v${snapshot.version}`
            : t("Map Library")}
        </span>
        <button
          className="icon-button"
          onClick={() => setLayers(!layers)}
          aria-label={t("Map layers")}
        >
          <Layers size={18} />
        </button>
      </div>
      <div className="map-bottom">
        <button onClick={fit}>
          <Maximize2 size={15} /> {t("Fit property")}
        </button>
        <button
          disabled={!positionVisible}
          title={
            snapshotId && snapshotMatches !== true
              ? t("Live position is hidden until the selected archive matches the active map")
              : !overlay
                ? t("Enable the live mower layer to use its position")
                : !live.position || !positionOnline
                  ? t("Live mower position is unavailable")
                  : undefined
          }
          onClick={() => {
            const p = displayPosition(live.position!);
            map.current?.setView([p.latitude, p.longitude], 20, {
              animate: false,
            });
          }}
        >
          <Crosshair size={15} /> {t("Mower")}
        </button>
      </div>
      {layers && (
        <div className="layer-panel">
          <label>
            {t("Basemap")}
            <select value={tiles} onChange={(e) => setTiles(e.target.value)}>
              <option value="satellite">{t("Satellite")}</option>
              <option value="none">{t("Offline / no tiles")}</option>
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              checked={overlay}
              onChange={(e) => setOverlay(e.target.checked)}
            />{" "}
            {historyId ? t("Saved route") : t("Live mower")}
          </label>
          <label>
            <input
              type="checkbox"
              checked={progress}
              onChange={(e) => setProgress(e.target.checked)}
            />{" "}
            {historyId ? t("Planned and completed path") : t("Live path / progress")}
          </label>
          {geo?.features.map((f, i) => {
            const id = String(f.properties?.source_id || f.properties?.kind);
            return (
              <label key={i}>
                <input
                  type="checkbox"
                  checked={
                    f.properties?.kind === "zone" || !hidden.includes(id)
                  }
                  disabled={f.properties?.kind === "zone"}
                  onChange={() =>
                    setHidden(
                      hidden.includes(id)
                        ? hidden.filter((x) => x !== id)
                        : [...hidden, id],
                    )
                  }
                />
                {f.properties?.name}
              </label>
            );
          })}
        </div>
      )}
      {error && (
        <div className="map-error" role="alert">
          {error}
        </div>
      )}
      {(snapshot || historyId) && overlay && !networkMode && (
        <div className="map-caption">
          {historyId
            ? t("Persistent job history shared by every browser")
            : snapshotMatches === true
            ? t("Archived base · live overlay matches this map")
            : snapshotMatches === false
              ? t("Archived base · live overlay hidden for a different map")
              : t("Archived base · checking the active map")}
          {progress && (
            <span className="coverage-legend">
              {routeStatus && <span>{routeStatus} · </span>}
              {coverageAvailable
                ? `${
                    coverageArea != null
                      ? `${t("Estimated mowed area")}: ${coverageArea.toLocaleString(undefined, { maximumFractionDigits: 1 })} m² · `
                      : ""
                  }${t(
                    "Green: estimated mowed area · Orange: traveled path · Yellow: remaining path",
                  )}`
                : t("Completed-area data not available yet")}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
export function validPosition(p: Position) {
  return (
    p.length >= 2 &&
    Number.isFinite(p[0]) &&
    Number.isFinite(p[1]) &&
    Math.abs(p[0]) <= 180 &&
    Math.abs(p[1]) <= 90
  );
}
