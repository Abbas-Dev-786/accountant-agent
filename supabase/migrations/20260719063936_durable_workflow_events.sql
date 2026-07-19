-- Durable task execution and review-package records for AccountingOS. These
-- schemas remain private; FastAPI applies organization authorization before
-- reading or changing any row.

create table workflow.task_events (
    id bigint generated always as identity primary key,
    organization_id text not null references workflow.organizations(id),
    run_id uuid not null references workflow.close_runs(id),
    task_id uuid references workflow.tasks(id),
    event_type text not null,
    payload_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index task_events_run_id_id_idx on workflow.task_events (run_id, id);

create table workflow.review_packages (
    id uuid primary key default gen_random_uuid(),
    organization_id text not null references workflow.organizations(id),
    run_id uuid not null unique references workflow.close_runs(id),
    snapshot_id uuid not null references normalized.source_snapshots(id),
    package_hash text not null unique,
    status text not null check (status in ('draft', 'review_frozen', 'finalized')),
    summary_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    frozen_at timestamptz
);

create table workflow.journal_proposals (
    id text primary key,
    organization_id text not null references workflow.organizations(id),
    run_id uuid not null references workflow.close_runs(id),
    review_package_id uuid not null references workflow.review_packages(id),
    journal_date date not null,
    narration text not null,
    proposal_hash text not null,
    status text not null check (status in ('proposed', 'approved', 'actioned', 'failed')),
    created_at timestamptz not null default now(),
    unique (run_id, proposal_hash)
);

create table workflow.journal_proposal_lines (
    proposal_id text not null references workflow.journal_proposals(id),
    line_number integer not null check (line_number > 0),
    account_code text not null,
    debit numeric(20, 4) not null default 0 check (debit >= 0),
    credit numeric(20, 4) not null default 0 check (credit >= 0),
    evidence_ids jsonb not null,
    primary key (proposal_id, line_number),
    check ((debit = 0) <> (credit = 0))
);

create or replace function workflow.validate_review_package_context()
returns trigger
language plpgsql
as $$
declare
    run workflow.close_runs%rowtype;
    snapshot normalized.source_snapshots%rowtype;
begin
    select * into run from workflow.close_runs where id = new.run_id;
    select * into snapshot from normalized.source_snapshots where id = new.snapshot_id;
    if run.id is null or snapshot.id is null or run.organization_id <> new.organization_id
       or snapshot.run_id <> new.run_id
       or snapshot.organization_id <> new.organization_id then
        raise exception 'review package must match its close run and source snapshot';
    end if;
    if tg_op = 'UPDATE' and (
        old.organization_id is distinct from new.organization_id
        or old.run_id is distinct from new.run_id
        or old.snapshot_id is distinct from new.snapshot_id
        or old.package_hash is distinct from new.package_hash
    ) then
        raise exception 'frozen review package identity cannot be changed';
    end if;
    if new.status = 'review_frozen' and new.frozen_at is null then
        new.frozen_at = now();
    end if;
    return new;
end;
$$;

create trigger review_packages_context_guard
before insert or update on workflow.review_packages
for each row execute function workflow.validate_review_package_context();

create or replace function workflow.validate_journal_proposal_context()
returns trigger
language plpgsql
as $$
declare
    review_package workflow.review_packages%rowtype;
begin
    select * into review_package from workflow.review_packages where id = new.review_package_id;
    if not found or review_package.organization_id <> new.organization_id
       or review_package.run_id <> new.run_id then
        raise exception 'journal proposal must belong to its review package close run';
    end if;
    if tg_op = 'UPDATE' and (
        old.organization_id is distinct from new.organization_id
        or old.run_id is distinct from new.run_id
        or old.review_package_id is distinct from new.review_package_id
        or old.proposal_hash is distinct from new.proposal_hash
    ) then
        raise exception 'journal proposal immutable context cannot be changed';
    end if;
    return new;
end;
$$;

create trigger journal_proposals_context_guard
before insert or update on workflow.journal_proposals
for each row execute function workflow.validate_journal_proposal_context();

alter table workflow.task_events enable row level security;
alter table workflow.review_packages enable row level security;
alter table workflow.journal_proposals enable row level security;
alter table workflow.journal_proposal_lines enable row level security;

revoke all on workflow.task_events, workflow.review_packages,
    workflow.journal_proposals, workflow.journal_proposal_lines
from anon, authenticated;
