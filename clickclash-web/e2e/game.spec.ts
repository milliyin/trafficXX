import { test, expect } from "@playwright/test";
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
  await page.getByRole("link", { name: "Leaderboard", exact: true }).click();
  await expect(page.getByText(nickname, { exact: true })).toBeVisible();
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
