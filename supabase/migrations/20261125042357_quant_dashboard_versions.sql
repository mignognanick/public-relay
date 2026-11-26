-- Isolated dashboard only. Apply to a development database, never the relay database.
-- Versions double as the append-only audit trail. No update/delete grants.
create table public.qe_versions (
    owner uuid not null references auth.users(id),
    version bigint not null check (version > 0),
    state jsonb not null check (
        jsonb_typeof(state) = 'object'
        and coalesce(state ->> 'execution' = 'PAPER', false)
        and octet_length(state::text) <= 2000000
    ),
    reason text not null check (char_length(btrim(reason)) between 3 and 600),
    created_at timestamptz not null default now(),
    primary key (owner, version)
);
alter table public.qe_versions enable row level security;
alter table public.qe_versions force row level security;
revoke all on public.qe_versions from public, anon, authenticated;
grant select, insert on public.qe_versions to authenticated;
create policy qe_read_own on public.qe_versions for select to authenticated
    using ((select auth.uid()) = owner);
create policy qe_append_own on public.qe_versions for insert to authenticated
    with check ((select auth.uid()) = owner and
        coalesce((select auth.jwt()) ->> 'is_anonymous', 'false') = 'false');

create function public.qe_check_version() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare previous bigint;
begin
    if new.owner is distinct from auth.uid() then
        raise exception 'Owner mismatch' using errcode = '42501';
    end if;
    select coalesce(max(version), 0) into previous from public.qe_versions where owner = new.owner;
    if new.version <> previous + 1 then
        raise exception 'Configuration version conflict' using errcode = '23505';
    end if;
    -- Same-version races are resolved by the primary key, even across serverless instances.
    new.created_at := clock_timestamp();
    return new;
end;
$$;
revoke all on function public.qe_check_version() from public, anon, authenticated;
create trigger qe_version_guard before insert on public.qe_versions
    for each row execute function public.qe_check_version();
comment on table public.qe_versions is
    'Paper dashboard configuration and immutable history. Not an execution authorization source. Future workers must independently validate state, approvals and risk.';
