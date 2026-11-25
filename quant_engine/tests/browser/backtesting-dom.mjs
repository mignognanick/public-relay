// DOM integration test against a local, fake-credential preview. No visual assertions.
import { JSDOM, VirtualConsole } from "jsdom";
import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import assert from "node:assert/strict";
import { Script } from "node:vm";
const base = "http://127.0.0.1:8766";
const errors = [];
const console = new VirtualConsole();
console.on("jsdomError", (error) => errors.push(error.message));
const dom = new JSDOM(
  readFileSync("quant_engine/dashboard/static/index.html", "utf8"),
  {
    url: base,
    runScripts: "outside-only",
    virtualConsole: console,
  },
);
const w = dom.window;
w.fetch = (path, options) => fetch(new URL(path, base), options);
w.crypto.randomUUID = randomUUID;
new Script(
  readFileSync("quant_engine/dashboard/static/app.js", "utf8"),
).runInContext(dom.getInternalVMContext());
new Script(
  readFileSync("quant_engine/dashboard/static/research.js", "utf8"),
).runInContext(dom.getInternalVMContext());
const find = (s) => w.document.querySelector(s);
const submit = (form) =>
  form.dispatchEvent(
    new w.Event("submit", { bubbles: true, cancelable: true }),
  );
async function until(predicate) {
  for (let i = 0; i < 200; i++) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 25));
  }
  throw new Error("Timed out: " + w.document.body.textContent.slice(-800));
}
find("[name=password]").value = "backtesting-local-test-token-123456789";
submit(find("#login-form"));
await until(() => !find("#workspace").hidden);
[...w.document.querySelectorAll("#nav button")]
  .find((b) => b.textContent === "Strategy Lab")
  .click();
find("[data-research-action=load]").click();
await until(() => find("[data-research-action=demo]"));
find("[data-research-action=demo]").click();
await until(() => find("[name=dataset_id] option"));
const originalVersion = find("#version").textContent;
const form = find("[data-research=run]");
form.elements.split_date.value = "2024-02-12";
form.querySelector("[type=checkbox]").checked = true;
submit(form);
await until(() => find("[data-research-action=download]"));
assert.match(find("#content").textContent, /SYNTHETIC DEMO/);
assert.match(
  find("#content").textContent,
  /Insufficient historical held-out evidence/,
);
assert.equal(find("#version").textContent, originalVersion);
assert.match(find("#content").textContent, /3\)/);
find("[data-research-action=load]").click();
await until(() => find("[data-research-action=report]"));
find("[data-research-action=report]").click();
await until(() => find("[data-research-action=download]"));
find("#logout").click();
assert.equal(find("#workspace").hidden, true);
assert.equal(find("#content").textContent, "");
assert.deepEqual(errors, []);
await new Promise((resolve) => setTimeout(resolve, 150));
assert.equal(find("#content").textContent, "");
dom.window.close();
process.stdout.write(
  "DOM integration: login, workbench, demo, run, report, refresh, unchanged registry and logout passed. Visual layout not tested.\n",
);
