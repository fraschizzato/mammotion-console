import { test, expect } from "@playwright/test";

test("native camera renders real image messages and keeps cockpit overlay", async ({
  page,
}) => {
  await page.route("**/api/camera/settings", (route) =>
    route.fulfill({
      json: {
        mode: "home_assistant",
        configured: { home_assistant: true, mammotion: true },
        connected: true,
        relay_supported: true,
        capability: { supported: true, protocol: "agora" },
      },
    }),
  );
  await page.routeWebSocket("**/api/camera/relay", async (ws) => {
    const jpeg = await page.evaluate(() => {
      const c = document.createElement("canvas");
      c.width = 64;
      c.height = 48;
      const ctx = c.getContext("2d")!;
      ctx.fillStyle = "#00ff00";
      ctx.fillRect(0, 0, 64, 48);
      return c.toDataURL("image/jpeg").split(",")[1];
    });
    ws.send(Buffer.from(jpeg, "base64"));
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Drive", exact: true }).click();
  const canvas = page.locator("canvas[data-native-frame][data-ready=true]");
  await expect(canvas).toBeVisible();
  await expect(page.locator(".camera-panel [role=status]")).toContainText(
    "Live",
  );
  expect(
    await canvas.evaluate((element: HTMLCanvasElement) => element.width),
  ).toBe(64);
  expect(
    await canvas.evaluate(
      (element: HTMLCanvasElement) =>
        element.getContext("2d")!.getImageData(5, 5, 1, 1).data[1],
    ),
  ).toBeGreaterThan(240);
});

test("HA task selectors display real options", async ({ page }) => {
  await page.route("**/api/jobs", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.capability.fields.ha_turning_mode = {
      name: "Modalità di svolta",
      selector: { select: { options: ["multipoint", "zero_turn"] } },
    };
    data.capability.fields.ha_bypass_mode = {
      name: "Rilevamento ostacoli",
      selector: {
        select: { options: ["direct_touch", "no_touch", "sensitive"] },
      },
    };
    await route.fulfill({ json: data });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Jobs", exact: true }).click();
  await page.getByLabel("Modalità di svolta").selectOption("multipoint");
  await page.getByLabel("Rilevamento ostacoli").selectOption("no_touch");
  await expect(page.getByLabel("Modalità di svolta")).toHaveValue("multipoint");
  await expect(page.getByLabel("Rilevamento ostacoli")).toHaveValue("no_touch");
  await expect(page.getByText("Not exposed by provider API")).toHaveCount(0);
});

test("HA Agora capability uses subscription viewer rather than native offer", async ({
  page,
}) => {
  await page.route("**/api/camera/settings", (route) =>
    route.fulfill({
      json: {
        mode: "home_assistant",
        configured: { home_assistant: true, mammotion: false },
        connected: true,
        capability: {
          supported: true,
          protocol: "agora",
          reason: "HA subscription",
        },
      },
    }),
  );
  await page.routeWebSocket("**/api/live", (socket) => {
    socket.send(
      JSON.stringify({
        connected: true,
        online: true,
        state: "docked",
        metrics: [],
        capabilities: {
          camera: {
            supported: true,
            protocol: "agora",
            reason: "HA subscription",
          },
          movement: { supported: false, reason: "Fixture" },
        },
      }),
    );
  });
  const sent: string[] = [];
  await page.routeWebSocket("**/api/camera", (socket) => {
    socket.onMessage((message) => sent.push(String(message)));
    socket.send(
      JSON.stringify({
        type: "error",
        message: "Fixture subscription response",
      }),
    );
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Drive", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(
    "Fixture subscription response",
  );
  expect(sent.some((message) => /"(config|offer)"/.test(message))).toBe(false);
});

test("camera waits for the selected video source before offering connection", async ({
  page,
}) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/camera/settings", async (route) => {
    await pending;
    await route.fulfill({
      json: {
        mode: "mammotion",
        configured: { mammotion: true, home_assistant: true },
        connected: true,
        capability: { supported: true, provider: "mammotion" },
      },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Drive", exact: true }).click();
  await expect(page.getByLabel("Video source", { exact: true })).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Connect camera", exact: true }),
  ).toHaveCount(0);
  release();
  await expect(page.getByLabel("Video source", { exact: true })).toHaveValue(
    "mammotion",
  );
  await expect(
    page.getByRole("button", { name: "Reconnect", exact: true }),
  ).toBeVisible();
});

test("video source switches independently and overview uses a product image", async ({
  page,
}) => {
  let mode = "home_assistant";
  let controlSwitches = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/api/connection") && request.method() === "PUT")
      controlSwitches++;
  });
  await page.route("**/api/camera/settings", async (route) => {
    if (route.request().method() === "PUT")
      mode = route.request().postDataJSON().mode;
    await route.fulfill({
      json: {
        mode,
        configured: { home_assistant: true, mammotion: true },
        connected: true,
        capability: {
          supported: true,
          protocol: "agora",
          provider: mode,
          reason: "Fixture",
        },
      },
    });
  });
  await page.goto("/");
  const photo = page.getByRole("img", {
    name: "Mammotion LUBA 3 AWD",
    exact: true,
  });
  await expect(photo).toBeVisible();
  await expect
    .poll(() =>
      photo.evaluate((image) => (image as HTMLImageElement).naturalWidth),
    )
    .toBeGreaterThan(0);
  await page.getByRole("button", { name: "Drive", exact: true }).click();
  await page
    .getByLabel("Video source", { exact: true })
    .selectOption("mammotion");
  await expect(page.getByLabel("Video source", { exact: true })).toHaveValue(
    "mammotion",
  );
  await page
    .getByLabel("Video source", { exact: true })
    .selectOption("home_assistant");
  expect(controlSwitches).toBe(0);
});

test("completed area has a distinct polygon overlay", async ({
  page,
  request,
}) => {
  const maps = await request
    .get("/api/maps")
    .then((response) => response.json());
  test.skip(!maps.length, "Requires an archived fixture map");
  const [west, south, east, north] = maps[0].manifest.bounds;
  await page.route("**/api/overlay**", (route) =>
    route.fulfill({
      json: {
        status: {
          stale: false,
          route_current: true,
          snapshot_matches: true,
        },
        traveled: {
          type: "FeatureCollection",
          features: [
            {
              type: "Feature",
              properties: { type_name: "dynamics_line" },
              geometry: {
                type: "LineString",
                coordinates: [
                  [west, south],
                  [east, north],
                ],
              },
            },
          ],
        },
        mowed: {
          type: "FeatureCollection",
          features: [
            {
              type: "Feature",
              properties: { estimated: true },
              geometry: {
                type: "Polygon",
                coordinates: [
                  [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                  ],
                ],
              },
            },
          ],
        },
      },
    }),
  );
  await page.goto("/");
  await expect(page.locator("path.mowed-area").first()).toBeAttached();
  await expect(page.locator('path[stroke="#f0a95b"]').first()).toBeAttached();
  await expect(
    page
      .getByText(
        "Green: estimated mowed area · Orange: traveled path · Yellow: remaining path",
      )
      .first(),
  ).toBeVisible();
});

test("live position and route stay hidden on a different archived map", async ({
  page,
  request,
}) => {
  const maps = await request
    .get("/api/maps")
    .then((response) => response.json());
  test.skip(!maps.length, "Requires an archived fixture map");
  const [west, south, east, north] = maps[0].manifest.bounds;
  let statusOnlyRequested = false;
  await page.routeWebSocket("**/api/live", (socket) =>
    socket.send(
      JSON.stringify({
        connected: true,
        online: true,
        position_online: true,
        state: "mowing",
        position: {
          latitude: (south + north) / 2,
          longitude: (west + east) / 2,
          direction: 45,
        },
        metrics: [],
        capabilities: {},
      }),
    ),
  );
  await page.route("**/api/overlay**", (route) => {
    statusOnlyRequested ||=
      new URL(route.request().url()).searchParams.get("include_route") === "false";
    return route.fulfill({
      json: {
        status: {
          stale: false,
          route_current: true,
          snapshot_matches: false,
        },
        traveled: {
          type: "FeatureCollection",
          features: [
            {
              type: "Feature",
              properties: {},
              geometry: {
                type: "LineString",
                coordinates: [
                  [west, south],
                  [east, north],
                ],
              },
            },
          ],
        },
      },
    });
  });
  await page.goto("/");
  await expect(
    page.getByText("Archived base · live overlay hidden for a different map"),
  ).toBeVisible();
  await expect(page.locator(".mower-marker")).toHaveCount(0);
  await expect(page.locator('path[stroke="#f0a95b"]')).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Mower", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Mower", exact: true }),
  ).toHaveAttribute(
    "title",
    "Live position is hidden until the selected archive matches the active map",
  );
  await page.getByRole("button", { name: "Maps", exact: true }).click();
  await page.getByRole("button", { name: "Map layers", exact: true }).click();
  await page.getByLabel("Live path / progress", { exact: true }).uncheck();
  await expect.poll(() => statusOnlyRequested).toBe(true);
  await expect(page.locator(".mower-marker")).toHaveCount(0);
});
