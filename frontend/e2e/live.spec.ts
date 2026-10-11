import { test, expect } from "@playwright/test";
test("mock HA updates telemetry and receives only confirmed scoped commands", async ({
  page,
  request,
}) => {
  test.skip(
    process.env.MOCK_HA !== "1",
    "Requires the explicit tests.mock_ha simulator",
  );
  await page.goto("/");
  await expect(page.locator(".connection")).toContainText("connected");
  await expect(page.locator(".state-row")).toContainText("63%");
  await page.getByRole("button", { name: "Maps", exact: true }).click();
  const maps = await request.get("/api/maps").then((r) => r.json());
  if (!maps.some((m: any) => m.device === "Luba-fixture")) {
    await page
      .getByRole("button", { name: "Capture map", exact: true })
      .click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(page.getByRole("dialog")).not.toBeVisible();
  }
  await expect(page.locator(".map-card").first()).toBeVisible();
  await page.getByRole("button", { name: "Jobs", exact: true }).click();
  await page.getByLabel("Orchard", { exact: true }).check();
  await page
    .getByRole("button", { name: "Review & start", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText("switch.luba_orchard");
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await page.getByRole("button", { name: "Drive", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "forward nudge", exact: true }),
  ).toBeDisabled();
  await expect(page.getByLabel("Mower minimap")).toBeVisible();
  await expect(page.locator('.camera-panel [role="status"]')).toContainText(
    "Test-only simulator has no camera media",
  );
  const diagnostic = await request.get("/api/diagnostics");
  expect(await diagnostic.text()).not.toContain("mock-ha-secret-canary");
});
