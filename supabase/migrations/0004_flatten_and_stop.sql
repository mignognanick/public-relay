-- Flatten sells every contract that is still open, and an optional stop can cover the position.
-- Apply after 0003_take_profit_and_exit_caps.sql. Replaces the open/close/get functions because
-- the argument lists changed; leftover overloads confuse PostgREST.

alter table public.open_positions
  add column if not exists tp_filled int not null default 0,
  add column if not exists stop_qty int not null default 0,
  add column if not exists stop_filled int not null default 0,
  add column if not exists stop_limit numeric(12,4),
  add column if not exists stop_order_id text,
  add column if not exists stop_status text not null default 'NONE';

alter table public.open_positions drop constraint if exists open_positions_tp_filled;
alter table public.open_positions add constraint open_positions_tp_filled check (tp_filled >= 0);
alter table public.open_positions drop constraint if exists open_positions_stop_qty;
alter table public.open_positions add constraint open_positions_stop_qty check (stop_qty >= 0);
alter table public.open_positions drop constraint if exists open_positions_stop_filled;
alter table public.open_positions add constraint open_positions_stop_filled check (stop_filled >= 0);

alter table public.open_positions drop constraint if exists open_positions_tp_status;
alter table public.open_positions add constraint open_positions_tp_status
  check (tp_status in ('NONE', 'RESTING', 'PARTIAL', 'CANCELLED', 'FILLED'));
alter table public.open_positions drop constraint if exists open_positions_stop_status;
alter table public.open_positions add constraint open_positions_stop_status
  check (stop_status in ('NONE', 'RESTING', 'PARTIAL', 'CANCELLED', 'FILLED'));

drop function if exists public.relay_get_position(text, text, text);
drop function if exists public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric);
drop function if exists public.relay_record_close(text, text, text, text, text);

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
        'stop_status', stop_status
      )
       from open_positions
      where underlying = p_underlying and strategy = p_strategy and mode = p_mode and status = 'OPEN'),
    'null'::jsonb);
$$;

create or replace function public.relay_record_open(
    p_underlying text, p_strategy text, p_mode text, p_symbol text, p_entry_side text,
    p_right_side text, p_qty int, p_strike numeric, p_alert_id text,
    p_qty_open int, p_tp_qty int, p_tp_limit numeric, p_tp_order_id text, p_tp_status text,
    p_entry_limit numeric, p_tp_filled int, p_stop_qty int, p_stop_filled int,
    p_stop_limit numeric, p_stop_order_id text, p_stop_status text)
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
      stop_qty, stop_filled, stop_limit, stop_order_id, stop_status)
  values (
      p_underlying, p_strategy, p_mode, p_symbol, p_entry_side, p_right_side, p_qty, p_strike,
      p_alert_id, now(), 'OPEN', null, null,
      p_qty_open, p_tp_qty, p_tp_filled, p_tp_limit, p_tp_order_id, p_tp_status, p_entry_limit,
      p_stop_qty, p_stop_filled, p_stop_limit, p_stop_order_id, p_stop_status)
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
      stop_status = excluded.stop_status;
  return null;
end $$;

-- Close the row and record the exit statuses after reconcile. A null status keeps the
-- previous value, cancelling a resting or partial order.
create or replace function public.relay_record_close(
    p_underlying text, p_strategy text, p_mode text, p_symbol text, p_alert_id text,
    p_tp_status text, p_stop_status text, p_tp_filled int, p_stop_filled int)
returns text language plpgsql security definer set search_path = public as $$
declare n int;
begin
  update open_positions set
      status = 'CLOSED',
      closed_alert_id = p_alert_id,
      closed_at = now(),
      qty_open = 0,
      tp_status = coalesce(p_tp_status, case when tp_status in ('RESTING', 'PARTIAL') then 'CANCELLED' else tp_status end),
      stop_status = coalesce(p_stop_status, case when stop_status in ('RESTING', 'PARTIAL') then 'CANCELLED' else stop_status end),
      tp_filled = coalesce(p_tp_filled, tp_filled),
      stop_filled = coalesce(p_stop_filled, stop_filled)
   where underlying = p_underlying and strategy = p_strategy and mode = p_mode
     and status = 'OPEN' and symbol = p_symbol;
  get diagnostics n = row_count;
  if n = 0 then
    return format('no open position for %s/%s', p_underlying, p_strategy);
  end if;
  return null;
end $$;

revoke all on function public.relay_get_position(text, text, text) from public, anon, authenticated;
revoke all on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text) from public, anon, authenticated;
revoke all on function public.relay_record_close(text, text, text, text, text, text, text, int, int) from public, anon, authenticated;
grant execute on function public.relay_get_position(text, text, text) to service_role;
grant execute on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text) to service_role;
grant execute on function public.relay_record_close(text, text, text, text, text, text, text, int, int) to service_role;
