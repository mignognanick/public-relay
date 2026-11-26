import { chromium } from "@playwright/test";
import assert from "node:assert/strict";
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
await page.goto("http://127.0.0.1:8765");
await page
  .locator("[name=password]")
  .fill("local-test-token-not-a-secret-123456789");
await page.getByRole("button", { name: "Open workspace" }).click();
await page.locator("#workspace").waitFor({ state: "visible" });
async function nav(name) {
  await page
    .getByRole("navigation")
    .getByRole("button", { name, exact: true })
    .click();
}
async function save(name) {
  await page.getByRole("button", { name, exact: true }).click();
  await page.locator("#message").filter({ hasText: "Saved version" }).waitFor();
}
for (const name of [
  "Overview",
  "Day Trading",
  "Swing Trading",
  "Long-Term Investing",
  "Strategy Lab",
  "Control Center",
  "Tax Center",
]) {
  await nav(name);
  assert.equal(await page.locator("#page-title").textContent(), name);
}
await nav("Day Trading");
await page.locator("[name=mode]").selectOption("manual");
await page.locator("[name=regime]").selectOption("bull");
await page.locator("[name=reason]").fill("Review bullish paper regime");
await save("Save portfolio settings");
await nav("Swing Trading");
assert.equal(await page.locator("[name=regime]").inputValue(), "chop");
await nav("Strategy Lab");
await page.locator("[name=name]").fill("<img src=x onerror=alert(1)> research");
await page
  .locator("[name=thesis]")
  .fill("Trade only after a closed bar, with independent stop sizing.");
await page
  .locator("form[data-action=create_strategy] [name=reason]")
  .fill("Register a research hypothesis");
await save("Add research idea");
assert.equal(await page.locator("#content img").count(), 0);
const research = page.locator("form[data-action=strategy]").last();
await research.locator("[name=stage]").selectOption("specified");
await research.locator("[name=reason]").fill("Rules reviewed");
await save("Save research review");
await research.locator("[name=stage]").selectOption("backtested");
await research
  .locator("[name=evidence]")
  .fill(
    "Dataset fixture; code abc123; train 2020-22, test 2023; fees .01; 35 OOS trades",
  );
await research.locator("[name=held_out_trades]").fill("35");
await research.locator("[name=trials]").fill("4");
await research.locator("[name=costs_modeled]").check();
await research
  .locator("[name=reason]")
  .fill("Record reproducible research evidence");
await save("Save research review");
for (const stage of ["validated", "paper"]) {
  await research.locator("[name=stage]").selectOption(stage);
  await research.locator("[name=reason]").fill("Human review of " + stage);
  if (stage === "paper") await research.locator("[name=enabled]").check();
  await save("Save research review");
}
await nav("Tax Center");
await page.locator("[name=short_term_net]").fill("20000");
await page.locator("[name=sufficient_income]").check();
await page
  .locator("[name=reason]")
  .fill("Explore federal gain offset scenario");
await save("Save and calculate scenario");
assert.match(await page.locator("#content").textContent(), /\$12,000\.00/);
await nav("Control Center");
assert.match(await page.locator("#content").textContent(), /local-operator/);
await page.screenshot({
  path: process.env.QE_SCREENSHOT || "/private/tmp/qe-control.png",
  fullPage: true,
});
await page.setViewportSize({ width: 390, height: 844 });
await nav("Overview");
assert.equal(
  await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth,
  ),
  true,
);
await page.getByRole("button", { name: "Refresh", exact: true }).click();
await page
  .locator("#message")
  .filter({ hasText: "Loaded the latest" })
  .waitFor();
await page.setViewportSize({ width: 1440, height: 1000 });
await page.getByRole("button", { name: "Sign out" }).click();
assert.equal(await page.locator("#content").textContent(), "");
assert.equal(
  await page.evaluate(() => localStorage.length + sessionStorage.length),
  0,
);
assert.deepEqual(errors, []);
await browser.close();
console.log(
  "Browser: all seven sections, isolated portfolio settings, research lifecycle, escaped input, tax scenario, audit, mobile layout and sign-out passed.",
);
