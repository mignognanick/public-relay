-- FlowVWAP resting limit entries, and idempotency keys for order emails.
-- Apply after 0006_flowvwap_levels.sql, before the relay that sends the new
-- arguments. QQQ rows leave the new columns null. The relay does not apply this file.
--
-- entry_placed_at is the app clock when the limit was sent (not the database
-- default), so a later monitor tick can time the order out. alert_underlying is
-- SPY at the alert. Stop and target stay null until the fill, then they are
-- written from SPY at fill time through relay_save_paper.

alter table public.open_positions
  add column if not exists entry_placed_at timestamptz,
  add column if not exists alert_underlying numeric(14,4);

create table if not exists public.notification_log (
  event_key text primary key,
  created_at timestamptz not null default now()
);

alter table public.notification_log enable row level security;

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
        'regime', regime,
        'entry_placed_at', entry_placed_at,
        'alert_underlying', alert_underlying
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
        'regime', regime,
        'entry_placed_at', entry_placed_at,
        'alert_underlying', alert_underlying
      ) order by underlying, strategy)
       from open_positions
      where mode = p_mode and status = 'OPEN'),
    '[]'::jsonb);
$$;

drop function if exists public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text, text, text, int, boolean, numeric, numeric, numeric, text);

create or replace function public.relay_record_open(
    p_underlying text, p_strategy text, p_mode text, p_symbol text, p_entry_side text,
    p_right_side text, p_qty int, p_strike numeric, p_alert_id text,
    p_qty_open int, p_tp_qty int, p_tp_limit numeric, p_tp_order_id text, p_tp_status text,
    p_entry_limit numeric, p_tp_filled int, p_stop_qty int, p_stop_filled int,
    p_stop_limit numeric, p_stop_order_id text, p_stop_status text,
    p_entry_order_id text, p_entry_status text, p_entry_filled int, p_exits_placed boolean,
    p_entry_underlying numeric default null, p_stop_underlying numeric default null,
    p_target_underlying numeric default null, p_regime text default null,
    p_entry_placed_at timestamptz default null, p_alert_underlying numeric default null)
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
      entry_underlying, stop_underlying, target_underlying, regime,
      entry_placed_at, alert_underlying)
  values (
      p_underlying, p_strategy, p_mode, p_symbol, p_entry_side, p_right_side, p_qty, p_strike,
      p_alert_id, now(), 'OPEN', null, null,
      p_qty_open, p_tp_qty, p_tp_filled, p_tp_limit, p_tp_order_id, p_tp_status, p_entry_limit,
      p_stop_qty, p_stop_filled, p_stop_limit, p_stop_order_id, p_stop_status,
      p_entry_order_id, coalesce(p_entry_status, 'NONE'), coalesce(p_entry_filled, 0), coalesce(p_exits_placed, false),
      p_entry_underlying, p_stop_underlying, p_target_underlying, p_regime,
      p_entry_placed_at, p_alert_underlying)
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
      regime = excluded.regime,
      entry_placed_at = excluded.entry_placed_at,
      alert_underlying = excluded.alert_underlying;
  return null;
end $$;

-- p_write_levels false leaves the FlowVWAP columns alone, so a QQQ save on a
-- database that has this function does not wipe them. Limit rows pass true.
drop function if exists public.relay_save_paper(text, text, text, text, text, int, int, int, numeric, text, text, int, int, numeric, text, text, boolean, int);

create or replace function public.relay_save_paper(
    p_underlying text, p_strategy text, p_mode text,
    p_entry_order_id text, p_entry_status text, p_entry_filled int,
    p_tp_qty int, p_tp_filled int, p_tp_limit numeric, p_tp_order_id text, p_tp_status text,
    p_stop_qty int, p_stop_filled int, p_stop_limit numeric, p_stop_order_id text, p_stop_status text,
    p_exits_placed boolean, p_qty_open int,
    p_write_levels boolean default false,
    p_entry_underlying numeric default null, p_stop_underlying numeric default null,
    p_target_underlying numeric default null,
    p_entry_placed_at timestamptz default null, p_alert_underlying numeric default null)
returns text language plpgsql security definer set search_path = public as $$
declare n int;
begin
  update open_positions set
      entry_order_id = p_entry_order_id,
      entry_status = p_entry_status,
      entry_filled = p_entry_filled,
      tp_qty = p_tp_qty,
      tp_filled = p_tp_filled,
      tp_limit = p_tp_limit,
      tp_order_id = p_tp_order_id,
      tp_status = p_tp_status,
      stop_qty = p_stop_qty,
      stop_filled = p_stop_filled,
      stop_limit = p_stop_limit,
      stop_order_id = p_stop_order_id,
      stop_status = p_stop_status,
      exits_placed = p_exits_placed,
      qty_open = p_qty_open,
      entry_underlying = case when p_write_levels then p_entry_underlying else entry_underlying end,
      stop_underlying = case when p_write_levels then p_stop_underlying else stop_underlying end,
      target_underlying = case when p_write_levels then p_target_underlying else target_underlying end,
      entry_placed_at = case when p_write_levels then p_entry_placed_at else entry_placed_at end,
      alert_underlying = case when p_write_levels then p_alert_underlying else alert_underlying end
   where underlying = p_underlying and strategy = p_strategy and mode = p_mode and status = 'OPEN';
  get diagnostics n = row_count;
  if n = 0 then
    return format('no open position for %s/%s', p_underlying, p_strategy);
  end if;
  return null;
end $$;

create or replace function public.relay_claim_notification(p_event_key text)
returns boolean language plpgsql security definer set search_path = public as $$
declare inserted int;
begin
  insert into notification_log (event_key) values (p_event_key)
  on conflict (event_key) do nothing;
  get diagnostics inserted = row_count;
  return inserted > 0;
end $$;

create or replace function public.relay_release_notification(p_event_key text)
returns void language plpgsql security definer set search_path = public as $$
begin
  delete from notification_log where event_key = p_event_key;
end $$;

revoke all on function public.relay_get_position(text, text, text) from public, anon, authenticated;
revoke all on function public.relay_list_open_positions(text) from public, anon, authenticated;
revoke all on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text, text, text, int, boolean, numeric, numeric, numeric, text, timestamptz, numeric) from public, anon, authenticated;
revoke all on function public.relay_save_paper(text, text, text, text, text, int, int, int, numeric, text, text, int, int, numeric, text, text, boolean, int, boolean, numeric, numeric, numeric, timestamptz, numeric) from public, anon, authenticated;
revoke all on function public.relay_claim_notification(text) from public, anon, authenticated;
revoke all on function public.relay_release_notification(text) from public, anon, authenticated;

grant execute on function public.relay_get_position(text, text, text) to service_role;
grant execute on function public.relay_list_open_positions(text) to service_role;
grant execute on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text, text, text, int, boolean, numeric, numeric, numeric, text, timestamptz, numeric) to service_role;
grant execute on function public.relay_save_paper(text, text, text, text, text, int, int, int, numeric, text, text, int, int, numeric, text, text, boolean, int, boolean, numeric, numeric, numeric, timestamptz, numeric) to service_role;
grant execute on function public.relay_claim_notification(text) to service_role;
grant execute on function public.relay_release_notification(text) to service_role;
