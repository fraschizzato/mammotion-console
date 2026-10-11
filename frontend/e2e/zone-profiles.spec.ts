import { test, expect } from "@playwright/test";

test("exact uint64 areas stay selectable and each zone keeps its own settings", async ({
  page,
}) => {
  const a = "2163392998022310366",
    b = "8607340384327153737",
    currentB = "3810263173152228334",
    c = "700515920207376663";
  const zones = [
    { source_id: a, name: "Zone A", geometry_fingerprint: "a".repeat(64) },
    {
      source_id: b,
      name: "Example Zone 04",
      geometry_fingerprint: "b".repeat(64),
    },
    {
      source_id: c,
      name: "Archived zone",
      geometry_fingerprint: "c".repeat(64),
    },
  ];
  const profiles: any[] = [];
  let started: any;
  let sequenceStarted: any;
  await page.routeWebSocket("**/api/live", (socket) =>
    socket.send(
      JSON.stringify({
        provider: "home_assistant",
        connected: true,
        online: true,
        state: "docked",
        error: "",
        device: { entity_id: "lawn_mower.test", name: "LUBA test" },
        metrics: [],
        position: null,
        capabilities: {},
      }),
    ),
  );
  await page.route("**/api/maps", (r) =>
    r.fulfill({
      json: [
        {
          id: "precision-map",
          name: "Property",
          version: 1,
          created: "2026-09-27T18:52:50Z",
          manifest: { zones, bounds: [10, 45, 10.001, 45.001] },
        },
        {
          id: "older-map",
          name: "Property",
          version: 1,
          created: "2026-09-19T17:24:56Z",
          manifest: {
            zones: [{ source_id: "old-a", name: "Old zone" }],
            bounds: [10, 45, 10.001, 45.001],
          },
        },
      ],
    }),
  );
  await page.route("**/api/maps/precision-map/geojson", (r) =>
    r.fulfill({
      json: {
        type: "FeatureCollection",
        features: zones.map((z) => ({
          type: "Feature",
          properties: { kind: "zone", source_id: z.source_id, name: z.name },
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [10, 45],
                [10.001, 45],
                [10.001, 45.001],
                [10, 45.001],
                [10, 45],
              ],
            ],
          },
        })),
      },
    }),
  );
  await page.route("**/api/jobs", (r) =>
    r.fulfill({
      json: {
        capability: {
          supported: true,
          reason: "Fixture",
          fields: {
            speed: { selector: { number: { min: 0.2, max: 1.2, step: 0.1 } } },
            toward: {
              name: "Cutting direction (degrees)",
              selector: { number: { min: -180, max: 180, step: 5 } },
            },
            toward_mode: { selector: { number: { min: 0, max: 2, step: 1 } } },
            areas: { selector: { entity: { multiple: true } } },
          },
        },
        zones: [
          {
            entity_id: "switch.a",
            hash: a,
            name: "Zone A",
            geometry_fingerprint: "a".repeat(64),
          },
          {
            entity_id: "switch.b",
            hash: currentB,
            name: "Example Zone 04",
            // Simulate both a volatile hash and a slightly shifted RTK-derived
            // geometry. The server-resolved stable name must preserve the link.
            geometry_fingerprint: "d".repeat(64),
          },
        ],
        zone_profiles: profiles,
        zone_sequences: [],
        presets: [
          {
            id: "preset-fast",
            name: "Fast lawn",
            map_id: "precision-map",
            fields: { speed: 0.9, areas: ["switch.a"] },
          },
        ],
        history: [],
      },
    }),
  );
  await page.route("**/api/jobs/zone-profile", (r) => {
    const body = r.request().postDataJSON();
    const saved = {
      ...body,
      id: body.zone_hash,
      geometry_fingerprint:
        body.zone_hash === currentB
          ? "b".repeat(64)
          : body.zone_fingerprint,
      zone_name:
        body.zone_hash === currentB
          ? "Example Zone 04"
          : zones.find(
              (z) =>
                z.source_id === body.zone_hash ||
                z.geometry_fingerprint === body.zone_fingerprint,
            )?.name,
    };
    const i = profiles.findIndex((p) => p.id === saved.id);
    if (i === -1) profiles.push(saved);
    else profiles[i] = saved;
    return r.fulfill({ json: saved });
  });
  await page.route("**/api/jobs/start", (r) => {
    started = r.request().postDataJSON();
    return r.fulfill({ json: { accepted: true } });
  });
  await page.route("**/api/jobs/zone-sequence", (r) => {
    sequenceStarted = r.request().postDataJSON();
    return r.fulfill({ json: { accepted: true, zone_count: 2 } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Jobs", exact: true }).click();
  await expect(page.getByLabel("Saved configuration")).toContainText("Fast lawn");
  await page.getByLabel("Task map").selectOption("precision-map");
  await expect(page.getByLabel("Speed slider")).toHaveAttribute("min", "0.2");
  await expect(page.getByLabel("Speed slider")).toHaveAttribute("max", "1.2");
  await expect(page.getByLabel("Speed slider")).toHaveAttribute("step", "0.1");
  await page.getByLabel("Cutting angle slider").fill("40");
  await expect(
    page.getByLabel("Cutting direction (degrees)", { exact: true }),
  ).toHaveValue("40");
  await expect(
    page.getByLabel("Task map").locator('option[value="precision-map"]'),
  ).toContainText("2/3 current zones");
  await expect(
    page.getByLabel("Task map").locator('option[value="older-map"]'),
  ).toContainText("0/1 current zones");
  await expect(
    page.getByRole("checkbox", { name: "Zone A", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("checkbox", { name: "Example Zone 04", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Configure zone: Zone A", exact: true })
    .click();
  await page.getByLabel("Speed", { exact: true }).fill("0.3");
  await page
    .getByRole("button", { name: "Save zone profile", exact: true })
    .click();
  await expect(page.getByLabel("Saved configuration")).toContainText("Zone A");
  await page
    .getByRole("button", { name: "Configure zone: Example Zone 04", exact: true })
    .click();
  await expect(page.getByLabel("Speed", { exact: true })).toHaveValue("");
  await page.getByLabel("Speed", { exact: true }).fill("0.7");
  await page
    .getByRole("button", { name: "Save zone profile", exact: true })
    .click();
  await expect(page.getByLabel("Saved configuration")).toContainText("Example Zone 04");
  await page.reload();
  await page.getByRole("button", { name: "Jobs", exact: true }).click();
  await expect(page.getByLabel("Task map")).toHaveValue("");
  await page
    .getByLabel("Saved configuration")
    .selectOption("preset:preset-fast");
  await expect(page.getByLabel("Task map")).toHaveValue("precision-map");
  await expect(page.getByLabel("Speed", { exact: true })).toHaveValue("0.9");
  await page.getByLabel("Saved configuration").selectOption(`zone:${a}`);
  await expect(page.getByLabel("Task map")).toHaveValue("precision-map");
  await expect(page.getByLabel("Speed", { exact: true })).toHaveValue("0.3");
  await expect(
    page.getByRole("checkbox", { name: "Zone A", exact: true }),
  ).toBeChecked();
  await expect(
    page.getByRole("checkbox", { name: "Example Zone 04", exact: true }),
  ).not.toBeChecked();
  await page
    .getByRole("button", { name: "Review & start", exact: true })
    .click();
  expect(started).toBeUndefined();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Confirm", exact: true })
    .click();
  await expect
    .poll(() => started)
    .toEqual({
      confirmed: true,
      map_id: "precision-map",
      fields: { speed: 0.3, areas: ["switch.a"] },
    });
  await page.getByRole("checkbox", { name: "Example Zone 04", exact: true }).check();
  await expect(
    page.getByRole("button", { name: "Start per-zone sequence", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Start per-zone sequence", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Confirm", exact: true })
    .click();
  await expect.poll(() => sequenceStarted).toEqual({
    confirmed: true,
    map_id: "precision-map",
    zone_hashes: [a, currentB],
  });
  await page
    .getByRole("button", { name: "Configure zone: Archived zone", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Review & start", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Speed", { exact: true }).fill("0.5");
  await page
    .getByRole("button", { name: "Save zone profile", exact: true })
    .click();
  await expect.poll(() => profiles.length).toBe(3);
  expect(profiles.map((p) => [p.zone_hash, p.fields.speed])).toEqual([
    [a, 0.3],
    [currentB, 0.7],
    [c, 0.5],
  ]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 2,
    ),
  ).toBe(true);
});
