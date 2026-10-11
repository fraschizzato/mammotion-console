/** Emitted only when the browser could not reach the console at all. */
export const networkFailureEvent = "mammotion-console:network-failure";
export const networkSuccessEvent = "mammotion-console:network-success";

export function reportNetworkFailure(error: unknown) {
  if (typeof window === "undefined" || !(error instanceof TypeError)) return;
  window.dispatchEvent(
    new CustomEvent(networkFailureEvent, {
      detail: { message: error instanceof Error ? error.message : String(error) },
    }),
  );
}

function reportNetworkSuccess() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(networkSuccessEvent));
  }
}

export async function api<T = any>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const form = body instanceof FormData;
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers: {
        "X-Console-Request": "1",
        ...(!form && body !== undefined
          ? { "Content-Type": "application/json" }
          : {}),
      },
      body: body === undefined ? undefined : form ? body : JSON.stringify(body),
    });
  } catch (error) {
    reportNetworkFailure(error);
    throw error;
  }
  reportNetworkSuccess();
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `Request failed (${response.status})`,
    );
  }
  return response.json();
}
export function socketUrl(path: string) {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api${path}`;
}
export type Snapshot = {
  id: string;
  name: string;
  notes: string;
  device: string;
  set_id: string;
  version: number;
  fingerprint: string;
  created: string;
  manifest: {
    bounds: number[];
    completeness: string;
    source: string;
    zones: {
      source_id: string;
      name: string;
      geometry_fingerprint?: string;
    }[];
  };
};
export type Capability = {
  supported: boolean;
  reason: string;
  fields?: Record<string, any>;
  protocol?: string;
  provider?: string;
  transports?: string[];
  bluetooth_error?: string;
};
export type Live = {
  network?: {
    ssid?: string | null;
    bssid?: string | null;
    connection: string;
    source: string;
    signals: Record<
      string,
      {
        dbm: number | null;
        quality: string;
        fresh: boolean;
        observed_at: number | null;
        source: string;
      }
    >;
  };
  position_online?: boolean;
  position_source?: string;
  mqtt?: { connected: boolean; report_age_seconds?: number };
  area_m2?: number | string | null;
  zone_name?: string | null;
  provider?: string;
  provider_label?: string;
  connected: boolean;
  online: boolean;
  error: string;
  state: string;
  device: { name: string; model: string; entity_id: string } | null;
  metrics: {
    id: string;
    key: string;
    name: string;
    value: string;
    unit: string;
  }[];
  position: {
    latitude: number;
    longitude: number;
    heading?: number;
    rotation?: number;
    direction?: number;
  } | null;
  capabilities: Record<string, Capability>;
};
export const emptyLive: Live = {
  connected: false,
  online: false,
  error: "Connecting to console…",
  state: "unavailable",
  device: null,
  metrics: [],
  position: null,
  capabilities: {},
};
