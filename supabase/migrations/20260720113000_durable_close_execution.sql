-- Durable close outputs and recovery state. These schemas are private and are
-- accessed only through the server-side Postgres connection.

create table workflow.reconciliations (
    run_id uuid primary key references workflow.close_runs(id),
    organization_id text not null references workflow.organizations(id),
    snapshot_id uuid not null references normalized.source_snapshots(id),
    input_hash text not null,
    result_hash text not null,
    matched_count integer not null check (matched_count >= 0),
    exception_count integer not null check (exception_count >= 0),
    created_at timestamptz not null default now()
);

create table workflow.reconciliation_matches (
    id text primary key,
    run_id uuid not null references workflow.reconciliations(run_id),
    organization_id text not null references workflow.organizations(id),
    match_kind text not null check (match_kind in ('exact', 'date_window', 'fee', 'aggregate')),
    amount numeric(20, 4) not null,
    currency text not null,
    bank_transaction_ids jsonb not null,
    ledger_transaction_ids jsonb not null,
    evidence_ids jsonb not null,
    created_at timestamptz not null default now()
);

create table workflow.reconciliation_exceptions (
    id text primary key,
    run_id uuid not null references workflow.reconciliations(run_id),
    organization_id text not null references workflow.organizations(id),
    control_code text not null,
    source_transaction_ids jsonb not null,
    evidence_ids jsonb not null,
    amount numeric(20, 4) not null,
    currency text not null,
    remediation text not null,
    status text not null default 'open' check (status in ('open', 'resolved', 'ignored')),
    explanation_json jsonb,
    explanation_status text not null default 'pending' check (explanation_status in ('pending', 'verified', 'rejected', 'unavailable')),
    explanation_updated_at timestamptz,
    resolution_comment text,
    resolved_by_subject text,
    resolved_at timestamptz,
    facts_json jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now()
);

create table workflow.close_reports (
    run_id uuid primary key references workflow.close_runs(id),
    organization_id text not null references workflow.organizations(id),
    snapshot_id uuid not null references normalized.source_snapshots(id),
    report_json jsonb not null,
    report_hash text not null,
    control_status text not null check (control_status in ('passed', 'exception', 'unavailable')),
    created_at timestamptz not null default now()
);

create table workflow.close_artifacts (
    id uuid primary key default gen_random_uuid(),
    organization_id text not null references workflow.organizations(id),
    run_id uuid not null references workflow.close_runs(id),
    artifact_type text not null check (artifact_type in ('close_package_json')),
    object_key text not null,
    content_hash text not null,
    retention_mode text not null check (retention_mode in ('compliance')),
    retain_until timestamptz not null,
    status text not null check (status in ('uploaded', 'verified', 'failed')),
    provider_file_id text,
    created_at timestamptz not null default now(),
    unique (run_id, artifact_type, content_hash)
);

create table workflow.recovery_email_requests (
    action_id uuid primary key references workflow.action_executions(id),
    run_id uuid not null references workflow.close_runs(id),
    exception_id text not null references workflow.reconciliation_exceptions(id),
    recipient text not null,
    created_at timestamptz not null default now(),
    unique (run_id, exception_id, recipient)
);

create index reconciliation_exceptions_run_status_idx
on workflow.reconciliation_exceptions (run_id, status, created_at);
create index close_artifacts_run_id_idx on workflow.close_artifacts (run_id, created_at);

create or replace function workflow.validate_reconciliation_context()
returns trigger
language plpgsql
as $$
declare
    run workflow.close_runs%rowtype;
    snapshot normalized.source_snapshots%rowtype;
begin
    select * into run from workflow.close_runs where id = new.run_id;
    select * into snapshot from normalized.source_snapshots where id = new.snapshot_id;
    if not found or run.organization_id <> new.organization_id
       or run.snapshot_id <> new.snapshot_id
       or snapshot.run_id <> new.run_id then
        raise exception 'reconciliation must match its frozen close run snapshot';
    end if;
    return new;
end;
$$;

create trigger reconciliations_context_guard
before insert on workflow.reconciliations
for each row execute function workflow.validate_reconciliation_context();

create or replace function workflow.reject_reconciliation_identity_change()
returns trigger
language plpgsql
as $$
begin
    if old.run_id is distinct from new.run_id or old.organization_id is distinct from new.organization_id
       or old.control_code is distinct from new.control_code or old.source_transaction_ids is distinct from new.source_transaction_ids
       or old.evidence_ids is distinct from new.evidence_ids or old.amount is distinct from new.amount
       or old.currency is distinct from new.currency or old.remediation is distinct from new.remediation
       or old.facts_json is distinct from new.facts_json then
        raise exception 'reconciliation evidence is immutable';
    end if;
    return new;
end;
$$;

create trigger reconciliation_exceptions_identity_guard
before update on workflow.reconciliation_exceptions
for each row execute function workflow.reject_reconciliation_identity_change();

create or replace function workflow.validate_close_artifact_context()
returns trigger
language plpgsql
as $$
declare
    run workflow.close_runs%rowtype;
begin
    select * into run from workflow.close_runs where id = new.run_id;
    if not found or run.organization_id <> new.organization_id then
        raise exception 'artifact must belong to its close run organization';
    end if;
    return new;
end;
$$;

create trigger close_artifacts_context_guard
before insert on workflow.close_artifacts
for each row execute function workflow.validate_close_artifact_context();

create or replace function workflow.validate_recovery_email_context()
returns trigger
language plpgsql
as $$
declare
    action workflow.action_executions%rowtype;
    exception workflow.reconciliation_exceptions%rowtype;
begin
    select * into action from workflow.action_executions where id = new.action_id;
    select * into exception from workflow.reconciliation_exceptions where id = new.exception_id;
    if action.id is null or exception.id is null or action.run_id <> new.run_id or exception.run_id <> new.run_id
       or action.provider <> 'gmail' or action.operation <> 'send_approved_request' then
        raise exception 'recovery email must bind one Gmail action to an exception in the same close run';
    end if;
    return new;
end;
$$;

create trigger recovery_email_requests_context_guard
before insert or update on workflow.recovery_email_requests
for each row execute function workflow.validate_recovery_email_context();

create trigger immutable_reconciliations
before update or delete on workflow.reconciliations
for each row execute function workflow.reject_immutable_change();
create trigger immutable_reconciliation_matches
before update or delete on workflow.reconciliation_matches
for each row execute function workflow.reject_immutable_change();
create trigger immutable_close_reports
before update or delete on workflow.close_reports
for each row execute function workflow.reject_immutable_change();
create trigger immutable_close_artifacts
before update or delete on workflow.close_artifacts
for each row execute function workflow.reject_immutable_change();
create trigger immutable_recovery_email_requests
before update or delete on workflow.recovery_email_requests
for each row execute function workflow.reject_immutable_change();

alter table workflow.reconciliations enable row level security;
alter table workflow.reconciliation_matches enable row level security;
alter table workflow.reconciliation_exceptions enable row level security;
alter table workflow.close_reports enable row level security;
alter table workflow.close_artifacts enable row level security;
alter table workflow.recovery_email_requests enable row level security;

revoke all on workflow.reconciliations, workflow.reconciliation_matches,
    workflow.reconciliation_exceptions, workflow.close_reports,
    workflow.close_artifacts, workflow.recovery_email_requests from anon, authenticated;
