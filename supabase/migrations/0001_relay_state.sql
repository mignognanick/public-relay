-- public-relay state: idempotency + per-day caps. Service-role only (RLS on, no policies).
create table if not exists public.processed_alerts (
  alert_id    text primary key,              -- unique alert_id => idempotency
  day         date not null,
  received_at timestamptz not null default now(),
  status      text not null default 'RECEIVED',
  open_close  text,
  notional    numeric(12,2) not null default 0,
  mode        text,
  detail      jsonb
);
create table if not exists public.daily_counters (
  day          date not null,
  mode         text not null,
  orders       int not null default 0,
  open_notional numeric(12,2) not null default 0,
  primary key (day, mode)
);
alter table public.processed_alerts enable row level security;
alter table public.daily_counters  enable row level security;

-- Atomic idempotency: true if newly reserved, false if duplicate.
create or replace function public.relay_reserve(p_alert_id text, p_day date)
returns boolean language plpgsql security definer set search_path = public as $$
declare n int;
begin
  insert into processed_alerts(alert_id, day) values (p_alert_id, p_day)
  on conflict (alert_id) do nothing;
  get diagnostics n = row_count;
  return n = 1;
end $$;

-- Atomic cap check + consume (row lock on the day/mode counter). Returns NULL if OK, else reason.
create or replace function public.relay_try_consume(p_day date, p_mode text, p_notional numeric,
    p_is_open boolean, p_max_orders int, p_max_opens numeric)
returns text language plpgsql security definer set search_path = public as $$
declare c daily_counters%rowtype;
begin
  insert into daily_counters(day, mode) values (p_day, p_mode) on conflict (day, mode) do nothing;
  select * into c from daily_counters where day = p_day and mode = p_mode for update;
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

-- Give back a consumed slot when preflight/order fails (only ACCEPTED counts, as in the sqlite version).
create or replace function public.relay_release(p_day date, p_mode text, p_notional numeric, p_is_open boolean)
returns void language sql security definer set search_path = public as $$
  update daily_counters set orders = greatest(orders - 1, 0),
         open_notional = greatest(open_notional - case when p_is_open then p_notional else 0 end, 0)
   where day = p_day and mode = p_mode;
$$;

revoke all on function public.relay_reserve(text, date) from public, anon, authenticated;
revoke all on function public.relay_try_consume(date, text, numeric, boolean, int, numeric) from public, anon, authenticated;
revoke all on function public.relay_release(date, text, numeric, boolean) from public, anon, authenticated;
grant execute on function public.relay_reserve(text, date) to service_role;
grant execute on function public.relay_try_consume(date, text, numeric, boolean, int, numeric) to service_role;
grant execute on function public.relay_release(date, text, numeric, boolean) to service_role;
