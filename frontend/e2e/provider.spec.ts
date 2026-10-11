import { test, expect } from "@playwright/test";

test("settings switches configured providers and reloads the dashboard", async ({ page }) => {
  let mode = "home_assistant";
  await page.route("**/api/settings", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.server = { ...data.server, connection_mode: mode, mammotion_configured: true, ha_configured: true, ha_base_url: "http://ha.test" };
    await route.fulfill({ json: data });
  });
  await page.route("**/api/connection", async (route) => {
    expect(route.request().method()).toBe("PUT");
    mode = route.request().postDataJSON().mode;
    await route.fulfill({ json: {} });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Active connection").selectOption("mammotion");
  await expect(page.getByRole("heading", { name: "A little closer to your lawn." })).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Active connection")).toHaveValue("mammotion");
  expect(mode).toBe("mammotion");
});
