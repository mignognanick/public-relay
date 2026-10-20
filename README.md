# public-relay on Vercel + Supabase (port, not deployed)
- `api/index.py` FastAPI app (all routes rewritten to it by `vercel.json`; region iad1, maxDuration 30s).
- State in Supabase: `supabase/migrations/0001_relay_state.sql` (processed_alerts, daily_counters,
  atomic RPCs relay_reserve / relay_try_consume / relay_release). Safe across concurrent instances; fails closed (503) if Supabase is unreachable.
- Public token cached per warm instance, refreshed 2 min before expiry or on 401.
- Tests: `pip install -r requirements.txt -r requirements-dev.txt && pytest -q tests` (Public + Supabase mocked).
- Endpoints: `GET /health`, `POST /webhook`. Same guardrails and PREFLIGHT/LIVE gating as the original.
