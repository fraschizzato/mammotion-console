import { test, expect } from "@playwright/test";

test("persistent mowing history opens on its archived map and reloads the planner", async ({
  page,
}) => {
  const map = {
    id: "history-map",
    name: "Property",
    version: 1,
    created: "2026-10-10T10:00:00Z",
    manifest: {
      bounds: [12, 43, 12.001, 43.001],
      zones: [{ source_id: "101", name: "Example Zone 04" }],
    },
  };
  const geometry = {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        properties: { kind: "zone", source_id: "101", name: "Example Zone 04" },
        geometry: {
          type: "Polygon",
          coordinates: [
            [
              [12, 43],
              [12.001, 43],
              [12.001, 43.001],
              [12, 43.001],
              [12, 43],
            ],
          ],
        },
      },
    ],
  };
  await page.routeWebSocket("**/api/live", (socket) =>
    socket.send(
      JSON.stringify({
        connected: true,
        online: true,
        state: "paused",
        device: { entity_id: "mammotion.test", name: "LUBA test" },
        metrics: [],
        position: null,
        capabilities: {},
      }),
    ),
  );
  await page.route("**/api/maps", (route) => route.fulfill({ json: [map] }));
  await page.route("**/api/maps/history-map/geojson", (route) =>
    route.fulfill({ json: geometry }),
  );
  await page.route("**/api/jobs", (route) =>
    route.fulfill({
      json: {
        capability: {
          supported: true,
          reason: "Fixture",
          fields: {
            areas: { selector: { entity: { multiple: true } } },
            speed: { selector: { number: { min: 0.2, max: 1.2, step: 0.1 } } },
          },
        },
        zones: [{ entity_id: "switch.zone_101", hash: "101", name: "Example Zone 04" }],
        presets: [],
        zone_profiles: [],
        zone_sequences: [],
        resume: { supported: true },
        history: [
          {
            id: "history-1",
            started: "2026-10-10T10:00:00Z",
            status: "recharging",
            fields: { areas: ["switch.zone_101"], speed: 0.5 },
            zones: [
              {
                entity_id: "switch.zone_101",
                name: "Example Zone 04",
                settings: { speed: 0.5 },
              },
            ],
            settings_note: "Reported by mower",
            route: {
              available: true,
              snapshot_id: "history-map",
              traveled_points: 3,
              mowed_area_m2: 12.4,
            },
          },
        ],
      },
    }),
  );
  await page.route("**/api/jobs/history/history-1/fields", (route) =>
    route.fulfill({ json: { fields: { areas: ["switch.zone_101"], speed: 0.5 } } }),
  );
  await page.route("**/api/jobs/history/history-1/overlay", (route) =>
    route.fulfill({
      json: {
        status: { historical: true, snapshot_matches: true },
        path: { type: "FeatureCollection", features: [] },
        progress: { type: "FeatureCollection", features: [] },
        traveled: {
          type: "FeatureCollection",
          features: [
            {
              type: "Feature",
              properties: { source: "mammotion_mqtt_position_trace" },
              geometry: {
                type: "LineString",
                coordinates: [
                  [12.0001, 43.0001],
                  [12.0003, 43.0003],
                  [12.0005, 43.0004],
                ],
              },
            },
          ],
        },
        mowed: { type: "FeatureCollection", features: [] },
      },
    }),
  );

  await page.goto("/");
  await page.getByRole("button", { name: "Jobs", exact: true }).click();
  await expect(page.getByText("Actual track: 3 points")).toBeVisible();
  await page.getByRole("button", { name: "View mowing map" }).click();
  await expect(page.getByRole("heading", { name: "Saved mowing pattern" })).toBeVisible();
  await expect(page.locator(".history-map .leaflet-overlay-pane path")).toHaveCount(2);

  await page.getByRole("button", { name: "Load in planner" }).click();
  await expect(page.getByLabel("Task map")).toHaveValue("history-map");
  await expect(page.getByRole("spinbutton", { name: "Speed" })).toHaveValue("0.5");

  await page.reload();
  await page.getByRole("button", { name: "Jobs", exact: true }).click();
  await page.getByRole("button", { name: "View mowing map" }).click();
  await expect(page.locator(".history-map .leaflet-overlay-pane path")).toHaveCount(2);
});
