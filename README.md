# public-relay on Vercel + Supabase (port, not deployed)
- `api/index.py` FastAPI app (all routes rewritten to it by `vercel.json`; region iad1, maxDuration 30s).
- State in Supabase: `supabase/migrations/0001_relay_state.sql` (processed_alerts, daily_counters,
  atomic RPCs relay_reserve / relay_try_consume / relay_release) and `0002_positions_and_picks.sql`
  (open_positions, contract_picks). Safe across concurrent instances; fails closed (503) if Supabase is unreachable.
- An alert can send a full OCC symbol, unchanged, or just an underlying (`SPY`). BUY selects a 0DTE call and
  SELL a 0DTE put, from ATM through 5 points ITM, with |delta| closest to 0.50–0.60. The limit is the bid/ask
  mid (ask when buying and mid is missing, bid when selling). A flatten, CLOSE, reversal, or opposite-side
  alert sells the stored contract instead of opening another. Apply `0002` before using underlying alerts;
  OCC alerts do not need it. Templates are in `sample_alert.json`. Defaults stay MODE=PREFLIGHT.
- Public token cached per warm instance, refreshed 2 min before expiry or on 401.
- Tests: `pip install -r requirements.txt -r requirements-dev.txt && pytest -q tests` (Public + Supabase mocked).
- Endpoints: `GET /health`, `POST /webhook`. Same guardrails and PREFLIGHT/LIVE gating as the original.
