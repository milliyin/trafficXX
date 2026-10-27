import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  retries: 0,
  outputDir: "../test-results/clickclash/browser",
  use: {
    baseURL: "http://127.0.0.1:8088",
    headless: true,
    launchOptions: process.env.CLICKCLASH_EDGE
      ? { executablePath: process.env.CLICKCLASH_EDGE }
      : {},
    screenshot: "only-on-failure",
    trace: "off",
  },
});
