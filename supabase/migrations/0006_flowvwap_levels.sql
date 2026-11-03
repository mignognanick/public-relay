-- FlowVWAP underlying exits and the daily UW / Daybreak levels.
-- Apply after 0005_paper_orders.sql, and before the relay that sends the new
-- record_open arguments. QQQ rows leave the new columns null.
-- Service-role only (RLS on, no policies). The relay does not apply this file.

alter table public.open_positions
  add column if not exists entry_underlying numeric(14,4),
  add column if not exists stop_underlying numeric(14,4),
  add column if not exists target_underlying numeric(14,4),
  add column if not exists regime text;

alter table public.open_positions drop constraint if exists open_positions_regime;
alter table public.open_positions add constraint open_positions_regime
  check (regime is null or regime in ('verified', 'unverified'));

create table if not exists public.daily_levels (
  day              date not null,
  symbol           text not null,
  flip             numeric(14,4),
  king             numeric(14,4),
  daybreak_bias    text,
  open_930         numeric(14,4),
  prior_rth_low    numeric(14,4),
  session_source   text,
  updated_at       timestamptz not null default now(),
  primary key (day, symbol)
);

alter table public.daily_levels enable row level security;

-- Same argument list as 0005. Extra position fields are added to the JSON only.
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
        'qty_open', qty_open,
        'strike', strike,
        'opened_alert_id', opened_alert_id,
        'status', status,
        'tp_qty', tp_qty,
        'tp_filled', tp_filled,
        'tp_limit', tp_limit,
        'tp_order_id', tp_order_id,
        'tp_status', tp_status,
        'entry_limit', entry_limit,
        'stop_qty', stop_qty,
        'stop_filled', stop_filled,
        'stop_limit', stop_limit,
        'stop_order_id', stop_order_id,
        'stop_status', stop_status,
        'entry_order_id', entry_order_id,
        'entry_status', entry_status,
        'entry_filled', entry_filled,
        'exits_placed', exits_placed,
        'entry_underlying', entry_underlying,
        'stop_underlying', stop_underlying,
        'target_underlying', target_underlying,
        'regime', regime
      )
       from open_positions
      where underlying = p_underlying and strategy = p_strategy and mode = p_mode and status = 'OPEN'),
    'null'::jsonb);
$$;

create or replace function public.relay_list_open_positions(p_mode text)
returns jsonb language sql stable security definer set search_path = public as $$
  select coalesce(
    (select jsonb_agg(jsonb_build_object(
        'underlying', underlying,
        'strategy', strategy,
        'mode', mode,
        'symbol', symbol,
        'entry_side', entry_side,
        'right_side', right_side,
        'qty', qty,
        'qty_open', qty_open,
        'strike', strike,
        'opened_alert_id', opened_alert_id,
        'status', status,
        'tp_qty', tp_qty,
        'tp_filled', tp_filled,
        'tp_limit', tp_limit,
        'tp_order_id', tp_order_id,
        'tp_status', tp_status,
        'entry_limit', entry_limit,
        'stop_qty', stop_qty,
        'stop_filled', stop_filled,
        'stop_limit', stop_limit,
        'stop_order_id', stop_order_id,
        'stop_status', stop_status,
        'entry_order_id', entry_order_id,
        'entry_status', entry_status,
        'entry_filled', entry_filled,
        'exits_placed', exits_placed,
        'entry_underlying', entry_underlying,
        'stop_underlying', stop_underlying,
        'target_underlying', target_underlying,
        'regime', regime
      ) order by underlying, strategy)
       from open_positions
      where mode = p_mode and status = 'OPEN'),
    '[]'::jsonb);
$$;

-- Argument list changed. Drop the 0005 overload so PostgREST has one function.
drop function if exists public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text, text, text, int, boolean);

create or replace function public.relay_record_open(
    p_underlying text, p_strategy text, p_mode text, p_symbol text, p_entry_side text,
    p_right_side text, p_qty int, p_strike numeric, p_alert_id text,
    p_qty_open int, p_tp_qty int, p_tp_limit numeric, p_tp_order_id text, p_tp_status text,
    p_entry_limit numeric, p_tp_filled int, p_stop_qty int, p_stop_filled int,
    p_stop_limit numeric, p_stop_order_id text, p_stop_status text,
    p_entry_order_id text, p_entry_status text, p_entry_filled int, p_exits_placed boolean,
    p_entry_underlying numeric default null, p_stop_underlying numeric default null,
    p_target_underlying numeric default null, p_regime text default null)
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
      opened_alert_id, opened_at, status, closed_alert_id, closed_at,
      qty_open, tp_qty, tp_filled, tp_limit, tp_order_id, tp_status, entry_limit,
      stop_qty, stop_filled, stop_limit, stop_order_id, stop_status,
      entry_order_id, entry_status, entry_filled, exits_placed,
      entry_underlying, stop_underlying, target_underlying, regime)
  values (
      p_underlying, p_strategy, p_mode, p_symbol, p_entry_side, p_right_side, p_qty, p_strike,
      p_alert_id, now(), 'OPEN', null, null,
      p_qty_open, p_tp_qty, p_tp_filled, p_tp_limit, p_tp_order_id, p_tp_status, p_entry_limit,
      p_stop_qty, p_stop_filled, p_stop_limit, p_stop_order_id, p_stop_status,
      p_entry_order_id, coalesce(p_entry_status, 'NONE'), coalesce(p_entry_filled, 0), coalesce(p_exits_placed, false),
      p_entry_underlying, p_stop_underlying, p_target_underlying, p_regime)
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
      closed_at = null,
      qty_open = excluded.qty_open,
      tp_qty = excluded.tp_qty,
      tp_filled = excluded.tp_filled,
      tp_limit = excluded.tp_limit,
      tp_order_id = excluded.tp_order_id,
      tp_status = excluded.tp_status,
      entry_limit = excluded.entry_limit,
      stop_qty = excluded.stop_qty,
      stop_filled = excluded.stop_filled,
      stop_limit = excluded.stop_limit,
      stop_order_id = excluded.stop_order_id,
      stop_status = excluded.stop_status,
      entry_order_id = excluded.entry_order_id,
      entry_status = excluded.entry_status,
      entry_filled = excluded.entry_filled,
      exits_placed = excluded.exits_placed,
      entry_underlying = excluded.entry_underlying,
      stop_underlying = excluded.stop_underlying,
      target_underlying = excluded.target_underlying,
      regime = excluded.regime;
  return null;
end $$;

-- Replace flip, king, and bias. Session columns already stored are left as they are.
create or replace function public.relay_upsert_levels(
    p_day date, p_symbol text, p_flip numeric, p_king numeric, p_daybreak_bias text)
returns void language plpgsql security definer set search_path = public as $$
begin
  insert into daily_levels (day, symbol, flip, king, daybreak_bias, updated_at)
  values (p_day, p_symbol, p_flip, p_king, p_daybreak_bias, now())
  on conflict (day, symbol) do update set
      flip = excluded.flip,
      king = excluded.king,
      daybreak_bias = excluded.daybreak_bias,
      updated_at = now();
end $$;

-- Fill the 9:30 open and the prior regular-hours low without wiping flip or king.
create or replace function public.relay_save_session(
    p_day date, p_symbol text, p_open_930 numeric, p_prior_rth_low numeric, p_session_source text)
returns void language plpgsql security definer set search_path = public as $$
begin
  insert into daily_levels (day, symbol, open_930, prior_rth_low, session_source, updated_at)
  values (p_day, p_symbol, p_open_930, p_prior_rth_low, p_session_source, now())
  on conflict (day, symbol) do update set
      open_930 = coalesce(excluded.open_930, daily_levels.open_930),
      prior_rth_low = coalesce(excluded.prior_rth_low, daily_levels.prior_rth_low),
      session_source = coalesce(excluded.session_source, daily_levels.session_source),
      updated_at = now();
end $$;

create or replace function public.relay_get_levels(p_day date, p_symbol text)
returns jsonb language sql stable security definer set search_path = public as $$
  select coalesce(
    (select jsonb_build_object(
        'day', day,
        'symbol', symbol,
        'flip', flip,
        'king', king,
        'daybreak_bias', daybreak_bias,
        'open_930', open_930,
        'prior_rth_low', prior_rth_low,
        'session_source', session_source
      )
       from daily_levels
      where day = p_day and symbol = p_symbol),
    'null'::jsonb);
$$;

revoke all on function public.relay_get_position(text, text, text) from public, anon, authenticated;
revoke all on function public.relay_list_open_positions(text) from public, anon, authenticated;
revoke all on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text, text, text, int, boolean, numeric, numeric, numeric, text) from public, anon, authenticated;
revoke all on function public.relay_upsert_levels(date, text, numeric, numeric, text) from public, anon, authenticated;
revoke all on function public.relay_save_session(date, text, numeric, numeric, text) from public, anon, authenticated;
revoke all on function public.relay_get_levels(date, text) from public, anon, authenticated;

grant execute on function public.relay_get_position(text, text, text) to service_role;
grant execute on function public.relay_list_open_positions(text) to service_role;
grant execute on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text, text, text, int, boolean, numeric, numeric, numeric, text) to service_role;
grant execute on function public.relay_upsert_levels(date, text, numeric, numeric, text) to service_role;
grant execute on function public.relay_save_session(date, text, numeric, numeric, text) to service_role;
grant execute on function public.relay_get_levels(date, text) to service_role;
