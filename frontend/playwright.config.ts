import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: process.env.CONSOLE_TEST_URL || "http://127.0.0.1:5000",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    {
      name: "mobile",
      use: { ...devices["iPhone 13"], defaultBrowserType: "chromium" },
    },
  ],
});
