import { test, expect } from "@playwright/test";

test("hibernating mower can be woken while offline in Italian", async ({
  page,
}) => {
  await page.addInitScript(() =>
    localStorage.setItem("console-language", "it"),
  );
  await page.routeWebSocket("**/api/live", (socket) =>
    socket.send(
      JSON.stringify({
        provider: "mammotion",
        connected: true,
        online: false,
        availability: "suspended",
        state: "hibernating",
        error: "",
        device: { entity_id: "mammotion.test", name: "LUBA test" },
        metrics: [],
        capabilities: {
          wake: {
            supported: true,
            reason: "Wake through Mammotion cloud; does not start mowing",
          },
        },
      }),
    ),
  );
  let requested = false;
  await page.route("**/api/mower/wake", (route) => {
    requested = route.request().postDataJSON().confirmed === true;
    return route.fulfill({ json: { accepted: true, pending: true } });
  });
  await page.goto("/");
  await expect(page.locator(".mower-card .pill")).toHaveText("Sospeso");
  await expect(page.locator(".state-row")).toContainText(
    "Sospeso — risveglio necessario",
  );
  const wake = page.getByRole("button", { name: /Sveglia/ });
  await expect(wake).toBeEnabled();
  await wake.click();
  expect(requested).toBe(false);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Conferma", exact: true })
    .click();
  await expect.poll(() => requested).toBe(true);
});

test("offline mower remains distinct from suspended and exposes safe wake", async ({
  page,
}) => {
  await page.routeWebSocket("**/api/live", (socket) =>
    socket.send(
      JSON.stringify({
        provider: "mammotion",
        connected: true,
        online: false,
        availability: "offline",
        state: "unavailable",
        error: "",
        device: { entity_id: "mammotion.test", name: "LUBA test" },
        metrics: [],
        capabilities: {
          wake: {
            supported: true,
            reason:
              "Try a Mammotion cloud wake for the offline mower; does not start mowing",
          },
        },
      }),
    ),
  );
  await page.goto("/");
  await expect(page.locator(".mower-card .pill")).toHaveText("Offline");
  await expect(page.locator(".state-row")).toContainText("Offline");
  await expect(page.locator(".state-row")).not.toContainText("Suspended");
  await expect(page.getByRole("button", { name: /wake/ })).toBeEnabled();
});

test("state, battery, zone mapping and Esri-only settings", async ({
  page,
}) => {
  await page.routeWebSocket("**/api/live", (socket) => {
    socket.send(
      JSON.stringify({
        provider: "home_assistant",
        connected: true,
        online: true,
        error: "",
        state: "mowing",
        device: {
          entity_id: "lawn_mower.test",
          name: "LUBA test",
          model: "LUBA 3",
        },
        area_m2: 789,
        zone_name: "Example Zone 01",
        metrics: [
          { id: "battery", key: "battery_percent", value: "58", unit: "%" },
        ],
        capabilities: {
          pause: { supported: true },
          resume: { supported: false },
          dock: { supported: true },
          stop: { supported: true },
        },
      }),
    );
  });
  await page.goto("/");
  await expect(page.getByLabel("Battery level")).toContainText("58%");
  await expect(page.locator(".mini-stats")).toContainText("Example Zone 01");
  await expect(
    page.locator(".control-grid button").filter({ hasText: "resume" }),
  ).toBeDisabled();
  await expect(
    page.locator(".control-grid button").filter({ hasText: "pause" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Basemap")).toHaveValue("satellite");
  await expect(
    page.getByLabel("Basemap").locator('option[value="street"]'),
  ).toHaveCount(0);
  await expect(page.getByLabel("Zone name", { exact: true })).toHaveCount(13);
  await expect(page.locator('input[value="Example Zone 01"]')).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Telegram notifications" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("map task shows a cutting direction diagram", async ({
  page,
  request,
}) => {
  test.skip(process.env.MOCK_HA !== "1", "Requires mock HA map and zone data");
  await page.route("**/api/jobs", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.capability.fields.toward = {
      selector: { number: { min: -180, max: 180, step: 1 } },
    };
    data.capability.fields.toward_mode = {
      selector: { select: { options: ["0", "1", "2"] } },
    };
    await route.fulfill({ json: data });
  });
  const maps = await request.get("/api/maps").then((r) => r.json());
  let map = maps.find((m: any) => m.device === "Luba-fixture");
  if (!map) {
    const response = await request.post("/api/maps/capture", {
      headers: { "X-Console-Request": "1" },
      data: { name: "Direction fixture" },
    });
    expect(response.ok()).toBeTruthy();
    map = await response.json();
  }
  await page.goto("/");
  await page.getByRole("button", { name: "Jobs", exact: true }).click();
  await page.getByLabel("Task map").selectOption(map.id);
  await page.getByLabel("Orchard", { exact: true }).check();
  await expect(
    page.getByRole("img", { name: /Indicative cutting direction/ }),
  ).toBeVisible();
  await expect(page.locator(".direction-preview path").first()).toBeAttached();
  await page.getByRole("button", { name: "Increase cutting angle" }).click();
  await expect(
    page.getByLabel("Cutting direction (degrees)", { exact: true }),
  ).toHaveValue("1");
  await expect(page.getByLabel("Direction mode")).toHaveValue("1");
  await page
    .getByLabel("Cutting direction (degrees)", { exact: true })
    .fill("42");
  await expect(page.getByLabel("Cutting angle", { exact: true })).toHaveText(
    "42°",
  );
  await page.getByRole("button", { name: "Decrease cutting angle" }).click();
  await expect(
    page.getByLabel("Cutting direction (degrees)", { exact: true }),
  ).toHaveValue("41");
  const order = await page.locator(".task-zones").boundingBox();
  const preview = await page.locator(".direction-preview").boundingBox();
  expect(
    order &&
      preview &&
      (order.x + order.width <= preview.x ||
        order.y + order.height <= preview.y),
  ).toBeTruthy();
  await page.getByLabel("Language", { exact: true }).selectOption("it");
  await expect(
    page.getByLabel("Direzione di taglio (gradi)", { exact: true }),
  ).toHaveValue("41");
  await expect(
    page.getByRole("heading", { name: "Ordine di taglio" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Aumenta angolo di taglio" }).click();
  await expect(
    page.getByLabel("Direzione di taglio (gradi)", { exact: true }),
  ).toHaveValue("42");
});

test("Italian language persists after reload", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Language", { exact: true }).selectOption("it");
  await expect(page.locator("html")).toHaveAttribute("lang", "it");
  await page.getByRole("button", { name: "Impostazioni", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Personalizza la console" }),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Lingua", { exact: true })).toHaveValue("it");
  await expect(
    page.getByRole("button", { name: "Panoramica", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByLabel("Lingua", { exact: true }).selectOption("en");
  await expect(
    page.getByRole("button", { name: "Overview", exact: true }),
  ).toBeVisible();
});
