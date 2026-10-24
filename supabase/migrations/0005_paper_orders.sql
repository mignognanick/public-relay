-- Paper mode stores the Alpaca entry and whether its exits have been placed.
-- Apply after 0004_flatten_and_stop.sql. Replaces relay_record_open because the
-- argument list changed; leftover overloads confuse PostgREST.

alter table public.open_positions
  add column if not exists entry_order_id text,
  add column if not exists entry_status text not null default 'NONE',
  add column if not exists entry_filled int not null default 0,
  add column if not exists exits_placed boolean not null default false;

alter table public.open_positions drop constraint if exists open_positions_entry_filled;
alter table public.open_positions add constraint open_positions_entry_filled check (entry_filled >= 0);

alter table public.open_positions drop constraint if exists open_positions_entry_status;
alter table public.open_positions add constraint open_positions_entry_status
  check (entry_status in ('NONE', 'PENDING', 'PARTIAL', 'FILLED', 'CANCELLED'));

-- Existing PREFLIGHT and LIVE rows stay on entry_status NONE, so a flatten still
-- sells the stored quantity. PAPER rows carry the Alpaca order id and its fills.

drop function if exists public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text);

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
        'exits_placed', exits_placed
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
    p_stop_limit numeric, p_stop_order_id text, p_stop_status text,
    p_entry_order_id text, p_entry_status text, p_entry_filled int, p_exits_placed boolean)
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
      entry_order_id, entry_status, entry_filled, exits_placed)
  values (
      p_underlying, p_strategy, p_mode, p_symbol, p_entry_side, p_right_side, p_qty, p_strike,
      p_alert_id, now(), 'OPEN', null, null,
      p_qty_open, p_tp_qty, p_tp_filled, p_tp_limit, p_tp_order_id, p_tp_status, p_entry_limit,
      p_stop_qty, p_stop_filled, p_stop_limit, p_stop_order_id, p_stop_status,
      p_entry_order_id, coalesce(p_entry_status, 'NONE'), coalesce(p_entry_filled, 0), coalesce(p_exits_placed, false))
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
      exits_placed = excluded.exits_placed;
  return null;
end $$;

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
        'exits_placed', exits_placed
      ) order by underlying, strategy)
       from open_positions
      where mode = p_mode and status = 'OPEN'),
    '[]'::jsonb);
$$;

create or replace function public.relay_save_paper(
    p_underlying text, p_strategy text, p_mode text,
    p_entry_order_id text, p_entry_status text, p_entry_filled int,
    p_tp_qty int, p_tp_filled int, p_tp_limit numeric, p_tp_order_id text, p_tp_status text,
    p_stop_qty int, p_stop_filled int, p_stop_limit numeric, p_stop_order_id text, p_stop_status text,
    p_exits_placed boolean, p_qty_open int)
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
      qty_open = p_qty_open
   where underlying = p_underlying and strategy = p_strategy and mode = p_mode and status = 'OPEN';
  get diagnostics n = row_count;
  if n = 0 then
    return format('no open position for %s/%s', p_underlying, p_strategy);
  end if;
  return null;
end $$;

revoke all on function public.relay_get_position(text, text, text) from public, anon, authenticated;
revoke all on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text, text, text, int, boolean) from public, anon, authenticated;
revoke all on function public.relay_list_open_positions(text) from public, anon, authenticated;
revoke all on function public.relay_save_paper(text, text, text, text, text, int, int, int, numeric, text, text, int, int, numeric, text, text, boolean, int) from public, anon, authenticated;
grant execute on function public.relay_get_position(text, text, text) to service_role;
grant execute on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric, int, int, int, numeric, text, text, text, text, int, boolean) to service_role;
grant execute on function public.relay_list_open_positions(text) to service_role;
grant execute on function public.relay_save_paper(text, text, text, text, text, int, int, int, numeric, text, text, int, int, numeric, text, text, boolean, int) to service_role;
