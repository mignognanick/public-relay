// DOM integration test for Alpha Center against a local fake-credential preview.
import { JSDOM, VirtualConsole } from "jsdom";
import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import assert from "node:assert/strict";
import { Script } from "node:vm";

const base = "http://127.0.0.1:8766";
const errors = [];
const vconsole = new VirtualConsole();
vconsole.on("jsdomError", (error) => errors.push(error.message));

const dom = new JSDOM(
  readFileSync("quant_engine/dashboard/static/index.html", "utf8"),
  {
    url: base,
    runScripts: "outside-only",
    virtualConsole: vconsole,
  },
);
const w = dom.window;
w.fetch = (path, options) => fetch(new URL(path, base), options);
w.crypto.randomUUID = randomUUID;

for (const asset of ["app.js", "research.js", "alpha.js"]) {
  new Script(
    readFileSync(`quant_engine/dashboard/static/${asset}`, "utf8"),
  ).runInContext(dom.getInternalVMContext());
}

const find = (s) => w.document.querySelector(s);
const submit = (form) =>
  form.dispatchEvent(
    new w.Event("submit", { bubbles: true, cancelable: true }),
  );

async function until(predicate) {
  for (let i = 0; i < 300; i++) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 30));
  }
  throw new Error("Timed out: " + w.document.body.textContent.slice(-800));
}

find("[name=password]").value = "backtesting-local-test-token-123456789";
submit(find("#login-form"));
await until(() => !find("#workspace").hidden);

const alphaNav = [...w.document.querySelectorAll("#nav button")].find(
  (b) => b.textContent === "Alpha Center",
);
assert.ok(alphaNav, "Alpha Center nav button missing");
alphaNav.click();
await until(() => find('[data-alpha="run-cycle"]'));
assert.match(find("#content").textContent, /151 Trading Strategies/);
assert.match(find("#content").textContent, /Research experiment queue/);

// Run controlled research cycle on synthetic demo
const runForm = find('[data-alpha="run-cycle"]');
runForm.elements.max_trials_per_hypothesis.value = "1";
submit(runForm);
await until(() => find("table.heatmap"));

const bodyText = find("#content").textContent;
assert.match(bodyText, /Strategy correlation heatmap/);
assert.match(bodyText, /Ensemble performance/);
assert.match(bodyText, /V3 · Kakushadze–Yu Alpha Combo \(Research Only\)/);
assert.match(bodyText, /Failed experiments & rejection reasons/);
assert.match(bodyText, /Market regime performance/);
assert.match(bodyText, /Current & proposed risk allocations/);

// Change allocation model through authenticated /api/commands control
const versionBefore = find("#version").textContent;
const modelForm = find('[data-action="allocation_model"]');
modelForm.elements.allocation_model.value = "v2";
modelForm.elements.reason.value = "Select V2 volatility and correlation model";
submit(modelForm);
await until(() => find("#version").textContent !== versionBefore);

// Sign out clears workspace and alpha state cleanly
find("#logout").click();
assert.equal(find("#workspace").hidden, true);
assert.equal(find("#content").textContent, "");
assert.deepEqual(errors, []);
dom.window.close();
process.stdout.write(
  "Alpha Center DOM integration: login, catalog, run-cycle, heatmap, ensembles, allocation model save and logout passed.\n",
);
