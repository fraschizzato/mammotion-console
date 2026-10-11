import { test, expect } from "@playwright/test";

test.skip(!process.env.MOCK_HA, "Uses an isolated mock database only");
test.beforeEach(async ({ request }) => {
  const points = await (await request.get("/api/network/access-points")).json();
  for (const point of points)
    if (point.name === "AP test courtyard")
      await request.delete(`/api/network/access-points/${point.id}`, {
        headers: { "X-Console-Request": "1" },
      });
});

test("AP map placement persists, edits indoor/outdoor and deletes", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Network", exact: true }).click();
  const canvas = page.locator(".network-page .map-canvas");
  await canvas.scrollIntoViewIfNeeded();
  await canvas.click({ position: { x: 170, y: 130 } });
  await page
    .getByRole("textbox", { name: "Name", exact: true })
    .fill("AP test courtyard");
  await page
    .getByRole("textbox", { name: "SSID", exact: true })
    .fill("Garden WiFi");
  await page
    .getByRole("textbox", { name: "BSSID / MAC AP", exact: true })
    .fill("AA:BB:CC:DD:EE:FF");
  await page.getByRole("button", { name: "Save access point" }).click();
  await expect(
    page.getByRole("button", { name: "Save access point" }),
  ).toHaveCount(0);
  await expect(page.locator(".access-point-marker")).toHaveCount(1);
  await page.reload();
  await page.getByRole("button", { name: "Network", exact: true }).click();
  await expect(page.locator(".access-point-marker")).toHaveCount(1);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Environment" })
    .selectOption("indoor");
  await page.getByRole("button", { name: "Save access point" }).click();
  await expect(
    page.getByRole("button", { name: "Save access point" }),
  ).toHaveCount(0);
  await expect(
    page.locator(".leaflet-tooltip").filter({ hasText: "AP test courtyard" }),
  ).toContainText("indoor");
  await page.getByRole("button", { name: "Delete access point" }).click();
  await expect(page.locator(".access-point-marker")).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 2,
    ),
  ).toBe(true);
});
