-- Quantitative Research Library and Alpha Combination Engine schema.
-- Review and apply to the isolated Quant Engine development database only; never the relay database.
-- All tables enforce owner-scoped RLS, anonymous denial, and PAPER-only research governance.

-- 1. Academic source references
create table public.qe_academic_sources (
    owner uuid not null references auth.users(id),
    source_id text not null check (char_length(btrim(source_id)) between 2 and 120),
    title text not null check (char_length(btrim(title)) between 2 and 300),
    authors jsonb not null check (jsonb_typeof(authors) = 'array'),
    publication_year integer not null check (publication_year between 1900 and 2100),
    primary_url text not null check (primary_url ~ '^https?://'),
    secondary_url text check (secondary_url is null or secondary_url ~ '^https?://'),
    section text not null check (char_length(btrim(section)) between 1 and 60),
    equation_numbers jsonb not null default '[]'::jsonb check (jsonb_typeof(equation_numbers) = 'array'),
    formula_summary text not null check (char_length(btrim(formula_summary)) between 3 and 4000),
    verified_notes text not null default '',
    adaptations jsonb not null default '[]'::jsonb check (jsonb_typeof(adaptations) = 'array'),
    created_at timestamptz not null default now(),
    primary key (owner, source_id)
);

-- 2. Research hypotheses
create table public.qe_research_hypotheses (
    owner uuid not null references auth.users(id),
    hypothesis_id text not null check (char_length(btrim(hypothesis_id)) between 2 and 120),
    source_id text not null,
    title text not null check (char_length(btrim(title)) between 3 and 240),
    thesis text not null check (char_length(btrim(thesis)) between 3 and 4000),
    portfolio text not null check (portfolio in ('day', 'swing', 'long_term')),
    strategy_key text not null check (char_length(btrim(strategy_key)) between 2 and 120),
    status text not null check (status in ('queued', 'running', 'recommended_for_paper', 'rejected', 'archived')),
    budget_used jsonb not null default '{}'::jsonb check (jsonb_typeof(budget_used) = 'object'),
    rejection_reasons jsonb not null default '[]'::jsonb check (jsonb_typeof(rejection_reasons) = 'array'),
    created_at timestamptz not null default now(),
    primary key (owner, hypothesis_id),
    foreign key (owner, source_id) references public.qe_academic_sources(owner, source_id)
);

-- 3. Alpha definitions and immutable versions
create table public.qe_alpha_definitions (
    owner uuid not null references auth.users(id),
    alpha_id text not null check (char_length(btrim(alpha_id)) between 2 and 120),
    version integer not null check (version > 0),
    strategy_key text not null check (char_length(btrim(strategy_key)) between 2 and 120),
    source_id text not null,
    portfolio text not null check (portfolio in ('day', 'swing', 'long_term')),
    execution text not null default 'PAPER' check (execution = 'PAPER'),
    deployment_status text not null check (
        deployment_status in ('research', 'candidate', 'paper_approved', 'paper_enabled', 'rejected', 'retired')
    ),
    research_only boolean not null default true,
    allow_inversion boolean not null default false,
    spec_hash text not null check (spec_hash ~ '^[a-f0-9]{64}$'),
    parameters jsonb not null default '{}'::jsonb check (jsonb_typeof(parameters) = 'object'),
    metadata jsonb not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz not null default now(),
    primary key (owner, alpha_id, version),
    foreign key (owner, source_id) references public.qe_academic_sources(owner, source_id)
);

-- 4. Experiment results
create table public.qe_experiment_results (
    owner uuid not null references auth.users(id),
    experiment_id text not null check (char_length(btrim(experiment_id)) between 2 and 120),
    hypothesis_id text,
    alpha_id text not null,
    alpha_version integer not null check (alpha_version > 0),
    dataset_id text not null check (char_length(btrim(dataset_id)) between 2 and 128),
    dataset_kind text not null check (dataset_kind in ('historical', 'synthetic')),
    status text not null check (status in ('queued', 'running', 'passed', 'failed', 'rejected')),
    trial_count integer not null check (trial_count >= 1),
    causality_passed boolean not null default false,
    costs_modeled boolean not null default true,
    held_out_trades integer not null default 0 check (held_out_trades >= 0),
    metrics jsonb not null default '{}'::jsonb check (jsonb_typeof(metrics) = 'object'),
    walk_forward jsonb not null default '{}'::jsonb check (jsonb_typeof(walk_forward) = 'object'),
    holdout jsonb not null default '{}'::jsonb check (jsonb_typeof(holdout) = 'object'),
    regime_performance jsonb not null default '{}'::jsonb check (jsonb_typeof(regime_performance) = 'object'),
    tax_impact jsonb not null default '{}'::jsonb check (jsonb_typeof(tax_impact) = 'object'),
    budget_cost jsonb not null default '{}'::jsonb check (jsonb_typeof(budget_cost) = 'object'),
    rejection_reasons jsonb not null default '[]'::jsonb check (jsonb_typeof(rejection_reasons) = 'array'),
    created_at timestamptz not null default now(),
    primary key (owner, experiment_id),
    foreign key (owner, alpha_id, alpha_version)
        references public.qe_alpha_definitions(owner, alpha_id, version)
);

-- 5. Daily alpha returns
create table public.qe_daily_alpha_returns (
    owner uuid not null references auth.users(id),
    alpha_id text not null,
    alpha_version integer not null check (alpha_version > 0),
    trade_date date not null,
    gross_return numeric(18, 10) not null,
    net_return numeric(18, 10) not null,
    turnover numeric(18, 10) not null default 0 check (turnover >= 0),
    regime text not null check (
        regime in ('strong_bull', 'bull', 'chop', 'bear', 'strong_bear', 'high_volatility')
    ),
    is_synthetic boolean not null default true,
    created_at timestamptz not null default now(),
    primary key (owner, alpha_id, alpha_version, trade_date),
    foreign key (owner, alpha_id, alpha_version)
        references public.qe_alpha_definitions(owner, alpha_id, version)
);

-- 6. Strategy correlation matrices
create table public.qe_correlation_matrices (
    owner uuid not null references auth.users(id),
    matrix_id text not null check (char_length(btrim(matrix_id)) between 2 and 120),
    as_of_date date not null,
    window_days integer not null check (window_days >= 5),
    alpha_ids jsonb not null check (jsonb_typeof(alpha_ids) = 'array'),
    correlation_matrix jsonb not null check (jsonb_typeof(correlation_matrix) = 'array'),
    covariance_matrix jsonb not null check (jsonb_typeof(covariance_matrix) = 'array'),
    shrinkage numeric(10, 6) not null check (shrinkage >= 0 and shrinkage <= 1),
    condition_number numeric(24, 6),
    diagnostics jsonb not null default '{}'::jsonb check (jsonb_typeof(diagnostics) = 'object'),
    created_at timestamptz not null default now(),
    primary key (owner, matrix_id)
);

-- 7. Allocation history
create table public.qe_allocation_history (
    owner uuid not null references auth.users(id),
    allocation_id text not null check (char_length(btrim(allocation_id)) between 2 and 120),
    model text not null check (model in ('v1', 'v2', 'v3')),
    execution text not null default 'PAPER' check (execution = 'PAPER'),
    research_only boolean not null default true,
    as_of_date date not null,
    weights jsonb not null check (jsonb_typeof(weights) = 'object'),
    previous_weights jsonb not null default '{}'::jsonb check (jsonb_typeof(previous_weights) = 'object'),
    turnover numeric(18, 10) not null default 0 check (turnover >= 0),
    constraints_applied jsonb not null default '{}'::jsonb check (jsonb_typeof(constraints_applied) = 'object'),
    diagnostics jsonb not null default '{}'::jsonb check (jsonb_typeof(diagnostics) = 'object'),
    reason text not null check (char_length(btrim(reason)) between 3 and 600),
    created_at timestamptz not null default now(),
    primary key (owner, allocation_id)
);

-- 8. Validation and approval status (append-only audit trail)
create table public.qe_validation_approvals (
    owner uuid not null references auth.users(id),
    approval_id text not null check (char_length(btrim(approval_id)) between 2 and 120),
    alpha_id text not null,
    alpha_version integer not null check (alpha_version > 0),
    experiment_id text not null,
    execution text not null default 'PAPER' check (execution = 'PAPER'),
    validation_status text not null check (
        validation_status in ('pending', 'validated', 'recommended_for_paper', 'paper_approved', 'rejected', 'revoked')
    ),
    paper_approved boolean not null default false,
    approved_by text,
    held_out_trades integer not null default 0 check (held_out_trades >= 0),
    trial_count integer not null default 1 check (trial_count >= 1),
    rejection_reasons jsonb not null default '[]'::jsonb check (jsonb_typeof(rejection_reasons) = 'array'),
    reason text not null check (char_length(btrim(reason)) between 3 and 600),
    created_at timestamptz not null default now(),
    primary key (owner, approval_id),
    check (not paper_approved or (approved_by is not null and char_length(btrim(approved_by)) >= 2 and held_out_trades >= 30)),
    foreign key (owner, alpha_id, alpha_version)
        references public.qe_alpha_definitions(owner, alpha_id, version),
    foreign key (owner, experiment_id)
        references public.qe_experiment_results(owner, experiment_id)
);

-- Shared timestamp trigger function
create function public.qe_alpha_timestamp() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
    if new.owner is distinct from auth.uid() then
        raise exception 'Owner mismatch' using errcode = '42501';
    end if;
    new.created_at := clock_timestamp();
    return new;
end;
$$;
revoke all on function public.qe_alpha_timestamp() from public, anon, authenticated;

-- Enable RLS, append-only grants, ownership policies, and triggers on all 8 tables
do $$
declare
    tbl text;
begin
    foreach tbl in array array[
        'qe_academic_sources',
        'qe_research_hypotheses',
        'qe_alpha_definitions',
        'qe_experiment_results',
        'qe_daily_alpha_returns',
        'qe_correlation_matrices',
        'qe_allocation_history',
        'qe_validation_approvals'
    ] loop
        execute format('alter table public.%I enable row level security', tbl);
        execute format('alter table public.%I force row level security', tbl);
        execute format('revoke all on public.%I from public, anon, authenticated', tbl);
        execute format('grant select, insert on public.%I to authenticated', tbl);
        execute format(
            'create policy %I on public.%I for select to authenticated using ((select auth.uid()) = owner)',
            tbl || '_read_own', tbl
        );
        execute format(
            'create policy %I on public.%I for insert to authenticated with check ((select auth.uid()) = owner and coalesce((select auth.jwt()) ->> ''is_anonymous'', ''false'') = ''false'')',
            tbl || '_append_own', tbl
        );
        execute format(
            'create trigger %I before insert on public.%I for each row execute function public.qe_alpha_timestamp()',
            tbl || '_stamp', tbl
        );
    end loop;
end;
$$;

create index qe_hypotheses_recent on public.qe_research_hypotheses(owner, created_at desc);
create index qe_experiments_recent on public.qe_experiment_results(owner, created_at desc);
create index qe_daily_returns_lookup on public.qe_daily_alpha_returns(owner, alpha_id, trade_date desc);
create index qe_corr_recent on public.qe_correlation_matrices(owner, as_of_date desc);
create index qe_alloc_recent on public.qe_allocation_history(owner, created_at desc);
create index qe_approvals_recent on public.qe_validation_approvals(owner, created_at desc);

comment on table public.qe_validation_approvals is
    'Append-only paper research validation and human approval audit log. Never authorizes live trading or direct brokerage execution.';
