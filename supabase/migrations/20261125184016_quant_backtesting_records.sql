-- Research records only. Review/apply to the dashboard database; no relay tables change.
create table public.qe_research_records (
  owner uuid not null references auth.users(id),
  kind text not null check (kind in ('dataset', 'trial', 'result')),
  id text not null check (char_length(id) between 1 and 100),
  payload jsonb not null check (jsonb_typeof(payload) = 'object' and octet_length(payload::text) <= 4000000),
  created_at timestamptz not null default now(),
  primary key (owner, kind, id)
);
alter table public.qe_research_records enable row level security;
alter table public.qe_research_records force row level security;
revoke all on public.qe_research_records from public, anon, authenticated;
grant select, insert on public.qe_research_records to authenticated;
create policy qe_research_read on public.qe_research_records for select to authenticated
  using ((select auth.uid()) = owner);
create policy qe_research_append on public.qe_research_records for insert to authenticated
  with check ((select auth.uid()) = owner and coalesce((select auth.jwt())->>'is_anonymous', 'false') = 'false');
create function public.qe_research_timestamp() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  new.created_at := clock_timestamp();
  return new;
end;
$$;
revoke all on function public.qe_research_timestamp() from public, anon, authenticated;
create trigger qe_research_stamp before insert on public.qe_research_records
  for each row execute function public.qe_research_timestamp();
create index qe_research_recent on public.qe_research_records(owner, kind, created_at desc);
comment on table public.qe_research_records is
  'Immutable research inputs, trial intents and results. User-owned research is not an execution authorization or independently verified performance record.';
