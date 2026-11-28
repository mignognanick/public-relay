-- Queue a TradingView alert so /webhook can ack before the order runs.
-- Apply after 0006_flowvwap_levels.sql, and before setting WEBHOOK_ASYNC.
-- While WEBHOOK_ASYNC is unset the relay does not call these functions.
-- Service-role only (RLS on, no policies). The relay does not apply this file.

create table if not exists public.relay_jobs (
  id           bigint generated always as identity primary key,
  alert_id     text not null,
  day          date not null,
  payload      jsonb not null,
  status       text not null default 'pending',
  attempts     int not null default 0,
  http_status  int,
  result       jsonb,
  received_at  timestamptz not null default now(),
  locked_at    timestamptz,
  finished_at  timestamptz,
  constraint relay_jobs_alert_id_key unique (alert_id),
  constraint relay_jobs_status check (status in ('pending', 'running', 'done', 'error')),
  constraint relay_jobs_attempts check (attempts >= 0 and attempts <= 3)
);

alter table public.relay_jobs enable row level security;

-- 'queued' when the alert is new, 'duplicate' when that alert_id was already stored.
-- Refuses a passphrase key so a secret cannot land in the row.
create or replace function public.relay_enqueue_job(p_alert_id text, p_day date, p_payload jsonb)
returns text language plpgsql security definer set search_path = public as $$
declare n int;
begin
  if p_alert_id is null or length(trim(p_alert_id)) = 0 then
    raise exception 'alert_id required';
  end if;
  if p_payload is null or jsonb_typeof(p_payload) <> 'object' then
    raise exception 'payload must be an object';
  end if;
  if p_payload ? 'passphrase' then
    raise exception 'payload must not include passphrase';
  end if;
  insert into public.relay_jobs (alert_id, day, payload)
  values (trim(p_alert_id), p_day, p_payload)
  on conflict (alert_id) do nothing;
  get diagnostics n = row_count;
  if n = 1 then
    return 'queued';
  end if;
  return 'duplicate';
end $$;

-- Claim a few pending jobs, or a running job whose lock is older than 90 seconds.
-- A third stale attempt is recorded as error and is not claimed again.
create or replace function public.relay_claim_jobs(p_limit int)
returns setof public.relay_jobs language plpgsql security definer set search_path = public as $$
declare n int := p_limit;
begin
  if n is null or n < 1 or n > 20 then
    n := 8;
  end if;
  update public.relay_jobs
     set status = 'error', finished_at = now()
   where status = 'running'
     and locked_at < now() - interval '90 seconds'
     and attempts >= 3;
  return query
  with picked as (
    select id
      from public.relay_jobs
     where (
       status = 'pending'
       or (status = 'running' and locked_at < now() - interval '90 seconds')
     )
       and attempts < 3
     order by received_at
     for update skip locked
     limit n
  )
  update public.relay_jobs as job
     set status = 'running',
         attempts = job.attempts + 1,
         locked_at = now()
    from picked
   where job.id = picked.id
  returning job.*;
end $$;

create or replace function public.relay_finish_job(
  p_id bigint, p_status text, p_http_status int, p_result jsonb)
returns void language plpgsql security definer set search_path = public as $$
begin
  if p_status is null or p_status not in ('pending', 'done', 'error') then
    raise exception 'invalid job status';
  end if;
  update public.relay_jobs
     set status = p_status,
         http_status = p_http_status,
         result = p_result,
         finished_at = case when p_status = 'pending' then null else now() end,
         locked_at = case when p_status = 'pending' then null else locked_at end
   where id = p_id;
end $$;

revoke all on function public.relay_enqueue_job(text, date, jsonb) from public, anon, authenticated;
revoke all on function public.relay_claim_jobs(int) from public, anon, authenticated;
revoke all on function public.relay_finish_job(bigint, text, int, jsonb) from public, anon, authenticated;
grant execute on function public.relay_enqueue_job(text, date, jsonb) to service_role;
grant execute on function public.relay_claim_jobs(int) to service_role;
grant execute on function public.relay_finish_job(bigint, text, int, jsonb) to service_role;
