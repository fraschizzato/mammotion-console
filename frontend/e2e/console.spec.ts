import { test, expect } from "@playwright/test";
test("offline library persists across map switching, navigation, and reload", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "A little closer to your lawn." }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Maps", exact: true }).click();
  const suffix = String(Date.now());
  const fixture = (name: string, id: string, lon: number) =>
    JSON.stringify({
      name: name + suffix,
      device: "browser-fixture-" + suffix,
      expected_zone_ids: [id],
      geojson: {
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            properties: { kind: "zone", source_id: id, name },
            geometry: {
              type: "Polygon",
              coordinates: [
                [
                  [lon, 45],
                  [lon + 0.001, 45],
                  [lon + 0.001, 45.001],
                  [lon, 45.001],
                  [lon, 45],
                ],
              ],
            },
          },
        ],
      },
    });
  await page.getByLabel("Import map").setInputFiles({
    name: "a.json",
    mimeType: "application/json",
    buffer: Buffer.from(fixture("Map A", "A", 9)),
  });
  await expect(
    page.getByRole("button", { name: new RegExp("Map A" + suffix) }),
  ).toBeVisible();
  await page.getByLabel("Import map").setInputFiles({
    name: "b.json",
    mimeType: "application/json",
    buffer: Buffer.from(fixture("Map B", "B", 10)),
  });
  await expect(
    page.getByRole("button", { name: new RegExp("Map B" + suffix) }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: new RegExp("Map A" + suffix) })
    .click();
  await expect(page.locator(".map-tag")).toContainText("Map A" + suffix);
  await expect(page.locator(".leaflet-interactive").first()).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Maps", exact: true }).click();
  await expect(
    page.getByRole("button", { name: new RegExp("Map A" + suffix) }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: new RegExp("Map B" + suffix) }),
  ).toBeVisible();
  for (const name of [
    "Jobs",
    "Schedules",
    "Drive",
    "Diagnostics",
    "Settings",
  ]) {
    await page.getByRole("button", { name, exact: true }).click();
    await expect(
      page.getByRole("heading", { name, exact: true, level: 1 }),
    ).toBeVisible();
  }
  expect(errors).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  const archived = await page.request.get("/api/maps").then((r) => r.json());
  for (const item of archived.filter(
    (m: any) => m.device === "browser-fixture-" + suffix,
  )) {
    await page.request.delete(`/api/maps/${item.id}?confirmed=true`, {
      headers: { "X-Console-Request": "1" },
    });
  }
});
