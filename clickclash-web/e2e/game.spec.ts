import { test, expect } from "@playwright/test";
import { readFileSync } from 'node:fs';

test('status pages separate measured failures from missing history', async ({page}) => {
  const stamp = new Date().toISOString();
  const components = [{name: 'Website', ok: true}, {name: 'Click API readiness', ok: true}, {name: 'Leaderboard', ok: true}];
  const snapshot = {checked_at: stamp, started_at: stamp, components, history: [
    {checked_at: stamp, ok: true},
    {checked_at: stamp, ok: false, components: components.map(item => ({...item, ok: item.name !== 'Leaderboard'}))},
    {checked_at: stamp, ok: true, components},
  ]};
  await page.route('https://milliyin.github.io/trafficXX/public-status/history.json', route => route.fulfill({json: snapshot}));
  await page.goto('/status');
  await expect(page.getByText('All monitored components operational')).toBeVisible();
  await expect(page.locator('.service-card')).toHaveCount(3);
  await expect(page.getByText('50.00% checks passed')).toBeVisible();
  await expect(page.locator('.uptime-bars .down')).toHaveCount(1);
  await expect(page.locator('.uptime-bars .unknown')).toHaveCount(264);
  await page.route('**/independent-status', route => route.fulfill({contentType: 'text/html', body: readFileSync('../docs/public-status/index.html', 'utf8')}));
  await page.route('**/history.json', route => route.fulfill({json: snapshot}));
  await page.goto('/independent-status');
  await expect(page.locator('.component')).toHaveCount(3);
  await expect(page.getByText('50.00% checks passed')).toBeVisible();
  await expect(page.locator('.bar.down')).toHaveCount(1);
  await expect(page.locator('.bar.unknown')).toHaveCount(264);
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
test("real guest clicks, signup, profile, leaderboard, logout/login and mobile", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "The leaderboard." }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Make your move" }).click();
  const click = page.getByRole("button", { name: "CLICK MAKE SOME NOISE" });
  await expect(click).toBeEnabled();
  await expect(page.getByText('Try auto-clicking')).toHaveCount(0);
  await click.click();
  await expect(page.locator(".own-score")).toHaveText("1");
  await click.click();
  await expect(page.locator(".own-score")).toHaveText("2");
  await page
    .getByRole("link", { name: "Create an account", exact: true })
    .click();
  const email = "browser-" + Date.now() + "@example.com";
  const nickname = "QA Player " + Date.now().toString().slice(-6);
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Public nickname").fill(nickname);
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Claim my spot" }).click();
  const phrase = await page.locator("output").textContent();
  expect(phrase?.split("-")).toHaveLength(5);
  await page.getByRole("button", { name: "I saved it" }).click();
  await page
    .getByLabel("Your bio")
    .fill("Browser test profile, not real player data.");
  await page.getByLabel("Website link").fill("https://example.com/");
  await page.getByRole("button", { name: "Save profile" }).click();
  await expect(page.getByRole("status")).toHaveText("Saved.");
  const largePicture = await page.evaluate(() => {
    const canvas = document.createElement('canvas');
    canvas.width = 3000;
    canvas.height = 2200;
    canvas.getContext('2d')!.fillRect(0, 0, 3000, 2200);
    return canvas.toDataURL('image/png').split(',')[1];
  });
  await page.getByLabel('Choose an image').setInputFiles({
    name: 'large-picture.png', mimeType: 'image/png', buffer: Buffer.from(largePicture, 'base64'),
  });
  await expect(page.getByAltText('Your promotion')).toBeVisible();
  await expect.poll(() => page.getByAltText('Your promotion').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
  await page.getByRole("link", { name: "Leaderboard", exact: true }).click();
  await expect(page.getByText(nickname, { exact: true })).toBeVisible();
  await expect(page.locator('.ad-cell img').first()).toBeVisible();
  await expect(
    page.getByText("Browser test profile, not real player data.").first(),
  ).toBeVisible();
  expect(await page.locator("body").textContent()).not.toContain(email);
  await page.screenshot({
    path: "../test-results/clickclash/desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../test-results/clickclash/mobile.png",
    fullPage: true,
  });
  await page.getByRole("link", { name: nickname }).click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.getByRole("link", { name: "Join the clash" }).click();
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Your passphrase").fill(phrase!);
  await page.getByRole("button", { name: "Welcome back" }).click();
  await expect(
    page.getByRole("heading", { name: "Make it yours." }),
  ).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete my account" }).click();
  await expect(
    page.getByRole("link", { name: "Join the clash" }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});
