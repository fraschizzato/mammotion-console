import { test, expect } from "@playwright/test";

test("schedules explain local profiles and link back to Jobs", async ({
  page,
}) => {
  await page.route("**/api/schedules", (r) =>
    r.fulfill({
      json: {
        schedules: [],
        capabilities: {},
        history: [],
        discovery: "unverified",
      },
    }),
  );
  await page.route("**/api/jobs", (r) =>
    r.fulfill({
      json: {
        zones: [],
        presets: [],
        zone_profiles: [],
        history: [],
        capability: { fields: {} },
      },
    }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "Schedules", exact: true }).click();
  await expect(
    page.getByText("Schedule list not verified", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Open saved jobs and profiles" })
    .click();
  await expect(
    page.getByText(
      "No local profiles saved yet. Configure a zone and save its profile, or select zones and save a preset.",
    ),
  ).toBeVisible();
  await page.getByLabel("Language", { exact: true }).selectOption("it");
  await page
    .getByRole("button", { name: "Pianificazioni", exact: true })
    .click();
  await expect(
    page.getByText("Elenco programmi non verificato", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Apri lavori e profili salvati" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Scegli le zone", exact: true }),
  ).toBeVisible();
});
