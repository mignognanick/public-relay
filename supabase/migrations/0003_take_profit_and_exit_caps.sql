-- Partial take-profit state and a separate daily exit cap.
-- Apply after 0002_positions_and_picks.sql, and before deploying the relay that sends the new RPC arguments.
-- Old function signatures are dropped: CREATE OR REPLACE cannot change arguments, and overloads confuse PostgREST.
-- OCC alerts call relay_try_consume, so this migration is required for every alert, not only underlying alerts.

alter table public.daily_counters
  add column if not exists exit_orders int not null default 0;

alter table public.open_positions
  add column if not exists qty_open int,
  add column if not exists tp_qty int not null default 0,
  add column if not exists tp_limit numeric(12,4),
  add column if not exists tp_order_id text,
  add column if not exists tp_status text not null default 'NONE',
  add column if not exists entry_limit numeric(12,4);

update public.open_positions set qty_open = qty where qty_open is null;
alter table public.open_positions alter column qty_open set not null;

alter table public.daily_counters drop constraint if exists daily_counters_exit_orders;
alter table public.daily_counters
  add constraint daily_counters_exit_orders check (exit_orders >= 0);

alter table public.open_positions drop constraint if exists open_positions_qty_open;
alter table public.open_positions add constraint open_positions_qty_open check (qty_open >= 0);

alter table public.open_positions drop constraint if exists open_positions_tp_qty;
alter table public.open_positions add constraint open_positions_tp_qty check (tp_qty >= 0);

alter table public.open_positions drop constraint if exists open_positions_tp_status;
alter table public.open_positions
  add constraint open_positions_tp_status check (tp_status in ('NONE', 'RESTING', 'CANCELLED', 'FILLED'));

drop function if exists public.relay_try_consume(date, text, numeric, boolean, int, numeric);
drop function if exists public.relay_release(date, text, numeric, boolean);
drop function if exists public.relay_get_position(text, text, text);
drop function if exists public.relay_record_open(text, text, text, text, text, text, int, numeric, text);
drop function if exists public.relay_record_close(text, text, text, text, text);

-- Atomic cap check + consume. Entries use orders / open_notional. Exits use exit_orders,
-- and also orders when p_exit_counts is true. Returns NULL if OK, else a reason.
create or replace function public.relay_try_consume(
    p_day date, p_mode text, p_notional numeric, p_is_open boolean, p_max_orders int, p_max_opens numeric,
    p_is_exit boolean default false, p_exit_counts boolean default false, p_max_exit_orders int default 6)
returns text language plpgsql security definer set search_path = public as $$
declare c daily_counters%rowtype;
begin
  insert into daily_counters(day, mode) values (p_day, p_mode) on conflict (day, mode) do nothing;
  select * into c from daily_counters where day = p_day and mode = p_mode for update;
  if p_is_exit then
    if c.exit_orders >= p_max_exit_orders then
      return format('MAX_EXIT_ORDERS_PER_DAY (%s) reached', p_max_exit_orders);
    end if;
    if p_exit_counts and c.orders >= p_max_orders then
      return format('MAX_ORDERS_PER_DAY (%s) reached', p_max_orders);
    end if;
    update daily_counters set
           exit_orders = exit_orders + 1,
           orders = orders + case when p_exit_counts then 1 else 0 end
     where day = p_day and mode = p_mode;
    return null;
  end if;
  if c.orders >= p_max_orders then
    return format('MAX_ORDERS_PER_DAY (%s) reached', p_max_orders);
  end if;
  if p_is_open and c.open_notional + p_notional > p_max_opens then
    return format('daily OPEN spend cap: spent $%s + $%s > $%s', c.open_notional, p_notional, p_max_opens);
  end if;
  update daily_counters set orders = orders + 1,
         open_notional = open_notional + case when p_is_open then p_notional else 0 end
   where day = p_day and mode = p_mode;
  return null;
end $$;

create or replace function public.relay_release(
    p_day date, p_mode text, p_notional numeric, p_is_open boolean,
    p_is_exit boolean default false, p_exit_counts boolean default false)
returns void language plpgsql security definer set search_path = public as $$
begin
  if p_is_exit then
    update daily_counters set
           exit_orders = greatest(exit_orders - 1, 0),
           orders = greatest(orders - case when p_exit_counts then 1 else 0 end, 0)
     where day = p_day and mode = p_mode;
    return;
  end if;
  update daily_counters set orders = greatest(orders - 1, 0),
         open_notional = greatest(open_notional - case when p_is_open then p_notional else 0 end, 0)
   where day = p_day and mode = p_mode;
end $$;

-- Null when flat. The open row otherwise (status OPEN only), including runner and take-profit fields.
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
        'tp_limit', tp_limit,
        'tp_order_id', tp_order_id,
        'tp_status', tp_status,
        'entry_limit', entry_limit
      )
       from open_positions
      where underlying = p_underlying and strategy = p_strategy and mode = p_mode and status = 'OPEN'),
    'null'::jsonb);
$$;

-- Insert or reopen. Returns null, or a reason if a position is already OPEN.
create or replace function public.relay_record_open(
    p_underlying text, p_strategy text, p_mode text, p_symbol text, p_entry_side text,
    p_right_side text, p_qty int, p_strike numeric, p_alert_id text,
    p_qty_open int, p_tp_qty int, p_tp_limit numeric, p_tp_order_id text, p_tp_status text,
    p_entry_limit numeric)
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
      qty_open, tp_qty, tp_limit, tp_order_id, tp_status, entry_limit)
  values (
      p_underlying, p_strategy, p_mode, p_symbol, p_entry_side, p_right_side, p_qty, p_strike,
      p_alert_id, now(), 'OPEN', null, null,
      p_qty_open, p_tp_qty, p_tp_limit, p_tp_order_id, p_tp_status, p_entry_limit)
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
      tp_limit = excluded.tp_limit,
      tp_order_id = excluded.tp_order_id,
      tp_status = excluded.tp_status,
      entry_limit = excluded.entry_limit;
  return null;
end $$;

-- Mark the stored contract closed. A resting take-profit is cancelled; its contracts are not sold here.
create or replace function public.relay_record_close(
    p_underlying text, p_strategy text, p_mode text, p_symbol text, p_alert_id text)
returns text language plpgsql security definer set search_path = public as $$
declare n int;
begin
  update open_positions set
      status = 'CLOSED',
      closed_alert_id = p_alert_id,
      closed_at = now(),
      qty_open = 0,
      tp_status = case when tp_status = 'RESTING' then 'CANCELLED' else tp_status end
   where underlying = p_underlying and strategy = p_strategy and mode = p_mode
     and status = 'OPEN' and symbol = p_symbol;
  get diagnostics n = row_count;
  if n = 0 then
    return format('no open position for %s/%s', p_underlying, p_strategy);
  end if;
  return null;
end $$;

revoke all on function public.relay_try_consume(date, text, numeric, boolean, int, numeric, boolean, boolean, int) from public, anon, authenticated;
revoke all on function public.relay_release(date, text, numeric, boolean, boolean, boolean) from public, anon, authenticated;
revoke all on function public.relay_get_position(text, text, text) from public, anon, authenticated;
revoke all on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric) from public, anon, authenticated;
revoke all on function public.relay_record_close(text, text, text, text, text) from public, anon, authenticated;
grant execute on function public.relay_try_consume(date, text, numeric, boolean, int, numeric, boolean, boolean, int) to service_role;
grant execute on function public.relay_release(date, text, numeric, boolean, boolean, boolean) to service_role;
grant execute on function public.relay_get_position(text, text, text) to service_role;
grant execute on function public.relay_record_open(text, text, text, text, text, text, int, numeric, text, int, int, numeric, text, text, numeric) to service_role;
grant execute on function public.relay_record_close(text, text, text, text, text) to service_role;
