-- Open relay positions (one per underlying + strategy + mode) and the contract-pick audit.
-- Service-role only (RLS on, no policies). Apply before sending underlying (non-OCC) alerts.
-- OCC alerts do not call these functions and keep working if this migration is not applied yet.

create table if not exists public.open_positions (
  underlying      text not null,
  strategy        text not null,
  mode            text not null,
  symbol          text not null,                 -- OCC symbol actually opened
  entry_side      text not null,                 -- alert side that opened: BUY (call) or SELL (put)
  right_side      text not null,                 -- C or P
  qty             int not null,
  strike          numeric(12,4) not null,
  opened_alert_id text not null,
  opened_at       timestamptz not null default now(),
  closed_alert_id text,
  closed_at       timestamptz,
  status          text not null default 'OPEN',
  primary key (underlying, strategy, mode),
  constraint open_positions_entry_side check (entry_side in ('BUY', 'SELL')),
  constraint open_positions_right check (right_side in ('C', 'P')),
  constraint open_positions_qty check (qty > 0),
  constraint open_positions_status check (status in ('OPEN', 'CLOSED'))
);

create table if not exists public.contract_picks (
  id                       bigint generated always as identity primary key,
  alert_id                 text not null,
  day                      date not null,
  mode                     text not null,
  action                   text not null,         -- open or close
  underlying               text,
  strategy                 text,
  underlying_price         numeric(14,4),
  underlying_price_source  text,                  -- alert or quote
  expiry                   date,
  candidates               jsonb not null,        -- strikes considered, with delta/bid/ask/mid
  chosen_strike            numeric(12,4),
  chosen_symbol            text,
  delta                    numeric(12,6),
  bid                      numeric(12,4),
  ask                      numeric(12,4),
  mid                      numeric(12,4),
  limit_price              numeric(12,4),
  limit_source             text,                  -- mid, ask (buy), or bid (sell)
  detail                   jsonb,
  created_at               timestamptz not null default now()
);
create index if not exists contract_picks_alert_id_idx on public.contract_picks (alert_id);

alter table public.open_positions enable row level security;
alter table public.contract_picks enable row level security;

-- Null when flat. The open row otherwise (status OPEN only).
create or replace function public.relay_get_position(p_underlying text, p_strategy text, p_mode text)
returns jsonb language sql stable security definer set search_path = public as $$
  select coalesce(
    (select jsonb_build_object(
        'underlying', underlying,
        'strategy', strategy,
        'mode', mode,
        'symbol', symbol,
        'entry_side', entry_side,
        'right_side', right_side,
        'qty', qty,
        'strike', strike,
        'opened_alert_id', opened_alert_id,
        'status', status
      )
       from open_positions
      where underlying = p_underlying and strategy = p_strategy and mode = p_mode and status = 'OPEN'),
    'null'::jsonb);
$$;

-- Insert or reopen. Returns null, or a reason if a position is already OPEN.
create or replace function public.relay_record_open(
    p_underlying text, p_strategy text, p_mode text, p_symbol text, p_entry_side text,
    p_right_side text, p_qty int, p_strike numeric, p_alert_id text)
returns text language plpgsql security definer set search_path = public as $$
declare existing open_positions%rowtype;
begin
  select * into existing from open_positions
   where underlying = p_underlying and strategy = p_strategy and mode = p_mode
   for update;
  if found and existing.status = 'OPEN' then
    return format('position already open for %s/%s', p_underlying, p_strategy);
  end if;
  insert into open_positions (
      underlying, strategy, mode, symbol, entry_side, right_side, qty, strike,
      opened_alert_id, opened_at, status, closed_alert_id, closed_at)
  values (
      p_underlying, p_strategy, p_mode, p_symbol, p_entry_side, p_right_side, p_qty, p_strike,
      p_alert_id, now(), 'OPEN', null, null)
  on conflict (underlying, strategy, mode) do update set
      symbol = excluded.symbol,
      entry_side = excluded.entry_side,
      right_side = excluded.right_side,
      qty = excluded.qty,
      strike = excluded.strike,
      opened_alert_id = excluded.opened_alert_id,
      opened_at = now(),
      status = 'OPEN',
      closed_alert_id = null,
      closed_at = null;
  return null;
end $$;

-- Mark the stored contract closed. Returns null, or a reason if it was not open.
create or replace function public.relay_record_close(
    p_underlying text, p_strategy text, p_mode text, p_symbol text, p_alert_id text)
returns text language plpgsql security definer set search_path = public as $$
declare n int;
begin
  update open_positions set status = 'CLOSED', closed_alert_id = p_alert_id, closed_at = now()
   where underlying = p_underlying and strategy = p_strategy and mode = p_mode
     and status = 'OPEN' and symbol = p_symbol;
  get diagnostics n = row_count;
  if n = 0 then
    return format('no open position for %s/%s', p_underlying, p_strategy);
  end if;
  return null;
end $$;

revoke all on function public.relay_get_position(text, text, text) from public, anon, authenticated;
revoke all on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text) from public, anon, authenticated;
revoke all on function public.relay_record_close(text, text, text, text, text) from public, anon, authenticated;
grant execute on function public.relay_get_position(text, text, text) to service_role;
grant execute on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text) to service_role;
grant execute on function public.relay_record_close(text, text, text, text, text) to service_role;
