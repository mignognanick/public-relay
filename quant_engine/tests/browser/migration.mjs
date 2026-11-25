// Run with the test-only npm dependencies documented in docs/DASHBOARD.md.
import { PGlite } from "@electric-sql/pglite";
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const db = new PGlite();
await db.exec(`create role anon; create role authenticated; create schema auth;
create table auth.users(id uuid primary key);
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
create function auth.jwt() returns jsonb language sql stable as $$ select coalesce(nullif(current_setting('request.jwt.claims',true),''),'{}')::jsonb $$;
grant usage on schema auth to authenticated, anon;
grant execute on all functions in schema auth to authenticated, anon;
insert into auth.users values ('00000000-0000-0000-0000-000000000001'), ('00000000-0000-0000-0000-000000000002');`);
await db.exec(
  readFileSync(
    "supabase/migrations/20261125042357_quant_dashboard_versions.sql",
    "utf8",
  ),
);
const owner = "00000000-0000-0000-0000-000000000001";
await db.exec(`set role authenticated; set request.jwt.claim.sub='${owner}';`);
async function insert(
  version = 1,
  actor = owner,
  state = { execution: "PAPER" },
) {
  return db.query(
    "insert into public.qe_versions(owner,version,state,reason) values ($1,$2,$3,$4)",
    [actor, version, JSON.stringify(state), "test change"],
  );
}
await insert();
assert.equal((await db.query("select * from qe_versions")).rows.length, 1);
await assert.rejects(() => insert(), /conflict/i);
await assert.rejects(() => insert(3), /conflict/i);
await assert.rejects(
  () => insert(2, owner, { execution: "LIVE" }),
  /check constraint/i,
);
await assert.rejects(() => insert(2, owner, {}), /check constraint/i);
await assert.rejects(
  () => insert(2, "00000000-0000-0000-0000-000000000002"),
  /Owner mismatch/i,
);
await assert.rejects(
  () => db.exec("delete from qe_versions"),
  /permission denied/i,
);
await assert.rejects(
  () => db.exec("update qe_versions set reason='tampered'"),
  /permission denied/i,
);
await insert(2);
await db.exec(
  "set request.jwt.claim.sub='00000000-0000-0000-0000-000000000002'",
);
assert.equal((await db.query("select * from qe_versions")).rows.length, 0);
await db.exec(
  `set request.jwt.claim.sub='${owner}'; set request.jwt.claims='{"is_anonymous":true}';`,
);
await assert.rejects(() => insert(3), /row-level security/i);
await db.exec("reset role; set role anon");
await assert.rejects(
  () => db.exec("select * from qe_versions"),
  /permission denied/i,
);
await db.exec("reset role; set request.jwt.claims='{}';");
await db.exec(
  readFileSync(
    "supabase/migrations/20261125184016_quant_backtesting_records.sql",
    "utf8",
  ),
);
await db.exec(`set role authenticated; set request.jwt.claim.sub='${owner}';`);
async function researchInsert(actor = owner, kind = "trial", id = "test") {
  return db.query(
    "insert into qe_research_records(owner,kind,id,payload) values ($1,$2,$3,$4)",
    [actor, kind, id, JSON.stringify({ test: true })],
  );
}
await researchInsert();
assert.equal(
  (await db.query("select * from qe_research_records")).rows.length,
  1,
);
await assert.rejects(() => researchInsert(), /duplicate/i);
await assert.rejects(
  () => researchInsert(owner, "invalid", "bad"),
  /check constraint/i,
);
await assert.rejects(
  () => researchInsert("00000000-0000-0000-0000-000000000002"),
  /row-level security/i,
);
await assert.rejects(
  () => db.exec("update qe_research_records set payload='{}'"),
  /permission denied/i,
);
await assert.rejects(
  () => db.exec("delete from qe_research_records"),
  /permission denied/i,
);
await db.exec(
  "set request.jwt.claim.sub='00000000-0000-0000-0000-000000000002'",
);
assert.equal(
  (await db.query("select * from qe_research_records")).rows.length,
  0,
);
await db.exec(
  `set request.jwt.claim.sub='${owner}'; set request.jwt.claims='{"is_anonymous":true}';`,
);
await assert.rejects(
  () => researchInsert(owner, "result", "anonymous"),
  /row-level security/i,
);
await db.exec("reset role; set role anon");
await assert.rejects(
  () => db.exec("select * from qe_research_records"),
  /permission denied/i,
);
console.log(
  "Research migration: ownership, immutable records, duplicate rejection, kind validation and anonymous denial passed.",
);
await db.close();
console.log(
  "Migration: ownership, RLS, append-only audit, version conflicts, anonymous denial and PAPER constraint passed.",
);
