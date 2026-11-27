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
await db.exec("reset role; set request.jwt.claims='{}';");
await db.exec(
  readFileSync(
    "supabase/migrations/20261126230000_quant_alpha_research.sql",
    "utf8",
  ),
);
await db.exec(`set role authenticated; set request.jwt.claim.sub='${owner}';`);
await db.query(
  "insert into public.qe_academic_sources(owner,source_id,title,authors,publication_year,primary_url,secondary_url,section,equation_numbers,formula_summary,adaptations) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
  [
    owner,
    "ks_8_1_hp_filter",
    "151 Trading Strategies",
    JSON.stringify(["Zura Kakushadze", "Juan Andrés Serur"]),
    2018,
    "https://papers.ssrn.com/abstract=3247865",
    "https://arxiv.org/pdf/1912.04492",
    "8.1",
    JSON.stringify(["437", "438", "439"]),
    "HP trend filter",
    JSON.stringify(["US equities causal filter"]),
  ],
);
await db.query(
  "insert into public.qe_research_hypotheses(owner,hypothesis_id,source_id,title,thesis,portfolio,strategy_key,status) values ($1,$2,$3,$4,$5,$6,$7,$8)",
  [owner, "hyp-hp", "ks_8_1_hp_filter", "HP Trend", "Causal HP trend", "swing", "hp_trend_filter", "queued"],
);
await db.query(
  "insert into public.qe_alpha_definitions(owner,alpha_id,version,strategy_key,source_id,portfolio,deployment_status,spec_hash) values ($1,$2,$3,$4,$5,$6,$7,$8)",
  [owner, "hp_spy", 1, "hp_trend_filter", "ks_8_1_hp_filter", "swing", "research", "a".repeat(64)],
);
await assert.rejects(
  () =>
    db.query(
      "insert into public.qe_alpha_definitions(owner,alpha_id,version,strategy_key,source_id,portfolio,execution,deployment_status,spec_hash) values ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
      [owner, "hp_live", 1, "hp_trend_filter", "ks_8_1_hp_filter", "swing", "LIVE", "research", "b".repeat(64)],
    ),
  /check constraint/i,
);
await db.query(
  "insert into public.qe_experiment_results(owner,experiment_id,hypothesis_id,alpha_id,alpha_version,dataset_id,dataset_kind,status,trial_count) values ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
  [owner, "exp-1", "hyp-hp", "hp_spy", 1, "ds-1", "synthetic", "rejected", 3],
);
await db.query(
  "insert into public.qe_daily_alpha_returns(owner,alpha_id,alpha_version,trade_date,gross_return,net_return,turnover,regime) values ($1,$2,$3,$4,$5,$6,$7,$8)",
  [owner, "hp_spy", 1, "2024-02-05", 0.002, 0.0018, 0.1, "bull"],
);
await db.query(
  "insert into public.qe_correlation_matrices(owner,matrix_id,as_of_date,window_days,alpha_ids,correlation_matrix,covariance_matrix,shrinkage) values ($1,$2,$3,$4,$5,$6,$7,$8)",
  [owner, "corr-1", "2024-02-05", 20, JSON.stringify(["hp_spy"]), JSON.stringify([[1]]), JSON.stringify([[0.0001]]), 0.25],
);
await db.query(
  "insert into public.qe_allocation_history(owner,allocation_id,model,as_of_date,weights,reason) values ($1,$2,$3,$4,$5,$6)",
  [owner, "alloc-1", "v1", "2024-02-05", JSON.stringify({ hp_spy: "1.0" }), "Initial equal risk test"],
);
await assert.rejects(
  () =>
    db.query(
      "insert into public.qe_validation_approvals(owner,approval_id,alpha_id,alpha_version,experiment_id,validation_status,paper_approved,approved_by,held_out_trades,reason) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
      [owner, "appr-bad", "hp_spy", 1, "exp-1", "paper_approved", true, "nick", 10, "Too few trades"],
    ),
  /check constraint/i,
);
await db.query(
  "insert into public.qe_validation_approvals(owner,approval_id,alpha_id,alpha_version,experiment_id,validation_status,paper_approved,approved_by,held_out_trades,reason) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
  [owner, "appr-1", "hp_spy", 1, "exp-1", "rejected", false, null, 10, "Synthetic dataset rejected"],
);
await assert.rejects(
  () => db.exec("delete from public.qe_validation_approvals"),
  /permission denied/i,
);
await assert.rejects(
  () => db.exec("update public.qe_alpha_definitions set deployment_status='retired'"),
  /permission denied/i,
);
await db.exec(
  "set request.jwt.claim.sub='00000000-0000-0000-0000-000000000002'",
);
assert.equal((await db.query("select * from public.qe_alpha_definitions")).rows.length, 0);
assert.equal((await db.query("select * from public.qe_experiment_results")).rows.length, 0);
await db.exec("reset role; set role anon");
await assert.rejects(
  () => db.exec("select * from public.qe_alpha_definitions"),
  /permission denied/i,
);
console.log(
  "Alpha research migration: all 8 tables, RLS isolation, append-only grants, PAPER constraint and approval guards passed.",
);
await db.close();
console.log(
  "Migration: ownership, RLS, append-only audit, version conflicts, anonymous denial and PAPER constraint passed.",
);
