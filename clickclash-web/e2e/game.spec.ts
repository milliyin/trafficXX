import { test, expect } from "@playwright/test";
import { readFileSync } from 'node:fs';

test('profile banner remains visible with the EasyList ad-cell hiding rule', async ({page}) => {
  await page.route('**/api/me', route => route.fulfill({status:401,json:{detail:{code:'SESSION_REQUIRED'}}}));
  await page.route('**/api/leaderboard*', route => route.fulfill({json:{
    entries:[{id:'profile-fixture',nickname:'Banner player',score:10,bio:'My profile',
      website:'https://example.com/',ad_hidden:false,image_url:'/api/images/profile-fixture'}],
    totals:{players:1,clicks:10},paused:false,offset:0,
  }}));
  await page.route('**/api/images/profile-fixture', route => route.fulfill({contentType:'image/png',
    body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII=', 'base64')}));
  await page.route('**/assets/*.css', async route => {
    const response = await route.fetch();
    await route.fulfill({response,body:(await response.text())+'\n.ad-cell {display:none!important}'});
  });
  await page.goto('/');
  await page.evaluate(() => {
    const bait = document.createElement('div');
    bait.className = 'ad-cell';
    bait.textContent = 'Cosmetic filter test';
    document.body.append(bait);
  });
  await expect(page.getByText('Cosmetic filter test')).toBeHidden();
  await expect(page.getByAltText('Banner player promotion')).toBeVisible();
  await expect.poll(() => page.getByAltText('Banner player promotion').evaluate((img:HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  await expect(page.locator('.profile-content')).toHaveAttribute('href','https://example.com/');
});

test('public profiles render as text and unsafe websites are not links', async ({page}) => {
  await page.route('**/api/me', route => route.fulfill({status:401,json:{detail:{code:'SESSION_REQUIRED'}}}));
  await page.route('**/api/leaderboard*', route => route.fulfill({json:{
    entries:[{id:'untrusted-profile', nickname:'<img src=x onerror=alert(1)>', score:1,
      bio:'<script>alert(1)</script>', website:'javascript:alert(1)', ad_hidden:false, image_url:null}],
    totals:{players:1,clicks:1}, paused:false, offset:0,
  }}));
  await page.goto('/');
  await expect(page.getByText('<img src=x onerror=alert(1)>', {exact:true})).toBeVisible();
  await expect(page.getByText('<script>alert(1)</script>', {exact:true})).toBeVisible();
  await expect(page.locator('.board-row a')).toHaveCount(0);
  await expect(page.locator('.board-row script,.board-row img')).toHaveCount(0);
});

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
  // Reproduce the EasyList cosmetic rule that hid the former container.
  // Serve as same-origin CSS, so the app's CSP remains enabled during the test.
  await page.route('**/assets/*.css', async route => {
    const response = await route.fetch();
    await route.fulfill({response, body: (await response.text()) + '\n.ad-cell { display: none !important; }'});
  });
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
  await expect(page.getByText('1200 × 200 pixels (6:1 ratio)')).toBeVisible();
  await page.getByLabel('Choose a banner image').setInputFiles({
    name: 'large-picture.png', mimeType: 'image/png', buffer: Buffer.from(largePicture, 'base64'),
  });
  await expect(page.getByAltText('Your promotion')).toBeVisible();
  await expect.poll(() => page.getByAltText('Your promotion').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
  await page.getByRole("link", { name: "Leaderboard", exact: true }).click();
  await expect(page.getByText(nickname, { exact: true })).toBeVisible();
  await expect(page.locator('.profile-cell img').first()).toBeVisible();
  const playerRow = page.locator('.board-row').filter({hasText: nickname});
  const promotion = playerRow.locator('.profile-content');
  const bannerBox = await promotion.locator('img').boundingBox();
  const promotionBox = await promotion.boundingBox();
  expect(bannerBox!.width).toBeGreaterThan(200);
  expect(bannerBox!.width / bannerBox!.height).toBeCloseTo(6, 1);
  expect(bannerBox!.width).toBeCloseTo(promotionBox!.width, 0);
  await expect(promotion).toHaveAttribute('href', 'https://example.com/');
  await expect(promotion).toHaveAttribute('target', '_blank');
  await expect(promotion).toHaveAttribute('rel', /noopener/);
  await page.context().route('https://example.com/**', route => route.fulfill({body: 'Linked promotion'}));
  for (const target of [playerRow.locator('.player-cell strong'), promotion.locator('img'), promotion.locator('p')]) {
    const popupPromise = page.waitForEvent('popup');
    await target.click();
    const popup = await popupPromise;
    await popup.waitForURL('https://example.com/');
    await popup.close();
  }
  await expect(playerRow.locator('.report')).toBeVisible();
  expect(await playerRow.locator('.report').evaluate(button => button.closest('a') === null)).toBe(true);
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
  await page.locator('a.account-link').click();
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
