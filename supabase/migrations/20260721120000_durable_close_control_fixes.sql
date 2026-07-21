-- Durable state for Plaid incremental sync plus controller-review control facts.
-- All tables remain private to the server-side PostgreSQL connection.

create table normalized.plaid_sync_states (
    organization_id text not null references workflow.organizations(id),
    item_id text not null,
    cursor text not null,
    records_json jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now(),
    primary key (organization_id, item_id),
    check (jsonb_typeof(records_json) = 'object')
);

create table workflow.plaid_sync_requests (
    id uuid primary key default gen_random_uuid(),
    organization_id text not null references workflow.organizations(id),
    item_id text not null,
    provider_event_id text not null unique,
    state text not null default 'ready' check (state in ('ready', 'running', 'succeeded', 'blocked', 'failed')),
    attempt integer not null default 0 check (attempt >= 0),
    max_attempts integer not null default 3 check (max_attempts between 1 and 10),
    lease_owner text,
    lease_expires_at timestamptz,
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index plaid_sync_requests_ready_idx
on workflow.plaid_sync_requests (state, created_at, id)
where state in ('ready', 'running');

create table workflow.evidence_checklist_evaluations (
    run_id uuid primary key references workflow.close_runs(id),
    organization_id text not null references workflow.organizations(id),
    checklist_id text not null,
    checklist_version integer not null check (checklist_version > 0),
    evidence_batch_id text not null,
    ready boolean not null,
    satisfied_json jsonb not null default '[]'::jsonb,
    missing_json jsonb not null default '[]'::jsonb,
    evaluated_at timestamptz not null default now(),
    check (jsonb_typeof(satisfied_json) = 'array' and jsonb_typeof(missing_json) = 'array')
);

create or replace function workflow.validate_evidence_checklist_context()
returns trigger
language plpgsql
as $$
declare
    run_organization_id text;
begin
    select organization_id into run_organization_id from workflow.close_runs where id = new.run_id;
    if run_organization_id is null or run_organization_id <> new.organization_id then
        raise exception 'evidence checklist evaluation must belong to its close run organization';
    end if;
    return new;
end;
$$;

create trigger evidence_checklist_context_guard
before insert or update on workflow.evidence_checklist_evaluations
for each row execute function workflow.validate_evidence_checklist_context();

alter table normalized.plaid_sync_states enable row level security;
alter table workflow.plaid_sync_requests enable row level security;
alter table workflow.evidence_checklist_evaluations enable row level security;

revoke all on normalized.plaid_sync_states, workflow.plaid_sync_requests,
    workflow.evidence_checklist_evaluations from anon, authenticated;
