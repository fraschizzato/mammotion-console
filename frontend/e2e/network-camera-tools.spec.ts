import { test, expect } from "@playwright/test";

test("camera snapshot, stop and restart close the previous stream", async ({
  page,
}) => {
  await page.route("**/api/camera/settings", (r) =>
    r.fulfill({
      json: {
        mode: "home_assistant",
        configured: { home_assistant: true, mammotion: true },
        connected: true,
        relay_supported: true,
        capability: { supported: true, protocol: "agora" },
      },
    }),
  );
  let opened = 0,
    closed = 0;
  await page.routeWebSocket("**/api/camera/relay", async (ws) => {
    opened++;
    ws.onClose(() => {
      closed++;
    });
    const frame = await page.evaluate(() => {
      const c = document.createElement("canvas");
      c.width = 96;
      c.height = 64;
      const ctx = c.getContext("2d")!;
      ctx.fillStyle = "#00ff00";
      ctx.fillRect(0, 0, 96, 64);
      return c.toDataURL("image/jpeg").split(",")[1];
    });
    await new Promise((resolve) => setTimeout(resolve, 250));
    ws.send(Buffer.from(frame, "base64"));
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Drive", exact: true }).click();
  const snapshot = page.getByRole("button", {
    name: "Take snapshot",
    exact: true,
  });
  await expect(snapshot).toBeDisabled();
  await expect(page.getByText("Starting live video…", { exact: true })).toBeVisible();
  await expect.poll(() => opened).toBe(1);
  await expect(snapshot).toBeEnabled();
  const download = page.waitForEvent("download");
  await snapshot.click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/^mammotion-.*\.jpg$/);
  const stream = await file.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  expect(Buffer.concat(chunks).subarray(0, 2).toString("hex")).toBe("ffd8");
  await page
    .getByRole("button", { name: "Stop streaming", exact: true })
    .click();
  await expect.poll(() => closed).toBe(1);
  await expect(snapshot).toBeDisabled();
  await expect(page.locator(".camera-panel [role=status]")).toHaveText(
    "Streaming stopped",
  );
  await page
    .getByRole("button", { name: "Connect camera", exact: true })
    .click();
  await expect.poll(() => opened).toBe(2);
  await expect(snapshot).toBeEnabled();
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await expect.poll(() => closed).toBe(2);
});

test("network heat map filters measured samples and handles missing history", async ({
  page,
}) => {
  await page.route("**/api/maps", (r) => r.fulfill({ json: [] }));
  await page.route("**/api/network/history?**", (r) => {
    const url = new URL(r.request().url());
    const cellular = url.searchParams.get("radio") === "cellular";
    return r.fulfill({
      json: {
        type: "FeatureCollection",
        devices: ["fixture"],
        device: "fixture",
        total_samples: cellular ? 0 : 4,
        retention_days: 90,
        truncated: false,
        features: cellular
          ? []
          : [
              {
                type: "Feature",
                geometry: { type: "Point", coordinates: [10, 45] },
                properties: {
                  quality: "weak",
                  mean_dbm: -81,
                  min_dbm: -84,
                  max_dbm: -79,
                  samples: 4,
                  last_observed: Date.now() / 1000,
                },
              },
            ],
      },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Network", exact: true }).click();
  await expect(page.locator(".network-heat-cell")).toHaveCount(1);
  await expect(
    page.getByText("4 samples · 1 measured cells", { exact: true }),
  ).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export heat map" }).click();
  expect((await download).suggestedFilename()).toBe(
    "mammotion-network-wifi-7d.geojson",
  );
  await page
    .getByRole("combobox", { name: "Radio", exact: true })
    .selectOption("cellular");
  await expect(page.locator(".network-heat-cell")).toHaveCount(0);
  await expect(
    page.getByText(/No fresh geolocated signal samples yet/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Export heat map" }),
  ).toBeDisabled();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 2,
    ),
  ).toBe(true);
});
