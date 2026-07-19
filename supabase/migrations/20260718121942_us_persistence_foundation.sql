-- US AccountingOS persistence foundation for Supabase Postgres.
-- Financial schemas are intentionally private: the browser talks to FastAPI,
-- never directly to the Supabase Data API or with a service-role key.

create extension if not exists pgcrypto;

create schema if not exists workflow;
create schema if not exists raw_xero_demo;
create schema if not exists raw_bank_demo;
create schema if not exists normalized;
create schema if not exists audit;

revoke all on schema workflow, raw_xero_demo, raw_bank_demo, normalized, audit
from anon, authenticated;

create table workflow.deployments (
    id text primary key,
    mode text not null check (mode in ('demo', 'production')),
    data_class text not null check (data_class in ('synthetic', 'live')),
    market text not null check (market in ('US', 'IN')),
    currency text not null,
    controller_subject text not null,
    created_at timestamptz not null default now(),
    check ((mode = 'demo' and data_class = 'synthetic' and market = 'US' and currency = 'USD')
        or (mode = 'production' and data_class = 'live'))
);

create table workflow.organizations (
    id text primary key,
    deployment_id text not null references workflow.deployments(id),
    name text not null,
    market text not null check (market in ('US', 'IN')),
    functional_currency text not null,
    accounting_timezone text not null,
    status text not null default 'active' check (status in ('active', 'suspended', 'deleted')),
    created_at timestamptz not null default now(),
    check ((market = 'US' and functional_currency = 'USD') or (market = 'IN' and functional_currency = 'INR'))
);

create table workflow.organization_users (
    organization_id text not null references workflow.organizations(id),
    identity_issuer text not null,
    identity_subject text not null,
    role text not null check (role in ('controller', 'operator', 'viewer')),
    created_at timestamptz not null default now(),
    primary key (organization_id, identity_issuer, identity_subject)
);

create table workflow.connections (
    id uuid primary key default gen_random_uuid(),
    organization_id text not null references workflow.organizations(id),
    provider text not null check (provider in ('xero', 'plaid', 'drive', 'gmail', 'b2', 'groq')),
    provider_environment text not null check (provider_environment in ('demo', 'sandbox', 'production')),
    provider_tenant_or_account_id text not null,
    credential_secret_ref text not null check (credential_secret_ref like 'secret://%'),
    status text not null check (status in ('connecting', 'healthy', 'delayed', 'partial', 'expired', 'revoked', 'failed', 'disconnected')),
    granted_scopes jsonb not null default '[]'::jsonb,
    last_verified_at timestamptz,
    last_success_at timestamptz,
    consent_expires_at timestamptz,
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table workflow.close_runs (
    id uuid primary key default gen_random_uuid(),
    organization_id text not null references workflow.organizations(id),
    deployment_id text not null references workflow.deployments(id),
    period_start date not null,
    period_end date not null,
    deployment_mode text not null check (deployment_mode in ('demo', 'production')),
    data_class text not null check (data_class in ('synthetic', 'live')),
    market text not null check (market in ('US', 'IN')),
    currency text not null,
    state text not null check (state in ('created', 'preflight', 'synchronizing', 'running', 'blocked', 'awaiting_input', 'awaiting_approval', 'changes_requested', 'applying_approved_actions', 'cancellation_requested', 'action_failed', 'approved', 'failed', 'cancelled')),
    snapshot_id uuid,
    package_hash text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (period_end >= period_start),
    check ((deployment_mode = 'demo' and data_class = 'synthetic' and market = 'US' and currency = 'USD')
        or (deployment_mode = 'production' and data_class = 'live'))
);

create table raw_xero_demo.records (
    id bigint generated always as identity primary key,
    organization_id text not null references workflow.organizations(id),
    run_id uuid references workflow.close_runs(id),
    source_batch_id uuid not null,
    tenant_id text not null,
    provider_record_id text not null,
    payload_json jsonb not null,
    content_hash text not null,
    observed_at timestamptz not null,
    request_id text,
    page_number integer not null check (page_number > 0),
    ingested_at timestamptz not null default now(),
    unique (source_batch_id, provider_record_id, content_hash)
);

create table raw_bank_demo.records (
    id bigint generated always as identity primary key,
    organization_id text not null references workflow.organizations(id),
    run_id uuid references workflow.close_runs(id),
    source_batch_id uuid not null,
    item_id text not null,
    account_id text not null,
    provider_record_id text not null,
    change_type text not null check (change_type in ('added', 'modified', 'removed')),
    payload_json jsonb not null,
    content_hash text not null,
    observed_at timestamptz not null,
    request_id text,
    cursor text,
    ingested_at timestamptz not null default now(),
    unique (source_batch_id, provider_record_id, change_type, content_hash)
);

create table normalized.source_batches (
    id uuid primary key default gen_random_uuid(),
    organization_id text not null references workflow.organizations(id),
    run_id uuid not null references workflow.close_runs(id),
    provider text not null,
    provider_environment text not null check (provider_environment in ('demo', 'sandbox', 'production')),
    watermark text not null,
    completed_at timestamptz not null,
    complete boolean not null default false,
    warnings jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now()
);

create table normalized.record_versions (
    version_id text primary key,
    source_batch_id uuid not null references normalized.source_batches(id),
    provider text not null,
    provider_record_id text not null,
    content_hash text not null,
    payload_json jsonb not null,
    observed_at timestamptz not null,
    currency text,
    accounting_date date,
    created_at timestamptz not null default now(),
    unique (source_batch_id, provider, provider_record_id, content_hash)
);

create table normalized.source_snapshots (
    id uuid primary key default gen_random_uuid(),
    organization_id text not null references workflow.organizations(id),
    run_id uuid not null unique references workflow.close_runs(id),
    deployment_id text not null references workflow.deployments(id),
    deployment_mode text not null check (deployment_mode in ('demo', 'production')),
    data_class text not null check (data_class in ('synthetic', 'live')),
    snapshot_cutoff_at timestamptz not null,
    source_batch_ids jsonb not null,
    status text not null check (status in ('building', 'complete', 'invalidated')),
    created_at timestamptz not null default now(),
    invalidated_at timestamptz,
    invalidation_reason text,
    check ((deployment_mode = 'demo' and data_class = 'synthetic') or deployment_mode = 'production')
);

create table normalized.snapshot_records (
    snapshot_id uuid not null references normalized.source_snapshots(id),
    normalized_record_version_id text not null references normalized.record_versions(version_id),
    source_batch_id uuid not null references normalized.source_batches(id),
    provider text not null,
    provider_record_id text not null,
    content_hash text not null,
    created_at timestamptz not null default now(),
    primary key (snapshot_id, normalized_record_version_id),
    unique (snapshot_id, provider, provider_record_id)
);

alter table workflow.close_runs
    add constraint close_runs_snapshot_fk
    foreign key (snapshot_id) references normalized.source_snapshots(id);

create table normalized.evidence_items (
    evidence_id text primary key,
    organization_id text not null references workflow.organizations(id),
    run_id uuid not null references workflow.close_runs(id),
    provider text not null check (provider in ('drive', 'gmail')),
    source_id text not null,
    content_hash text not null,
    observed_at timestamptz not null,
    kind text not null check (kind in ('document', 'email', 'attachment')),
    scope_reference text not null,
    tags jsonb not null default '[]'::jsonb,
    metadata_json jsonb not null default '{}'::jsonb,
    unique (run_id, provider, source_id)
);

create table workflow.tasks (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null references workflow.close_runs(id),
    task_key text not null,
    state text not null check (state in ('pending', 'ready', 'running', 'succeeded', 'blocked', 'failed', 'cancelled')),
    attempt integer not null default 0 check (attempt >= 0),
    lease_owner text,
    lease_expires_at timestamptz,
    last_error text,
    idempotency_key text not null unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (run_id, task_key)
);

create table workflow.task_dependencies (
    task_id uuid not null references workflow.tasks(id),
    depends_on_task_id uuid not null references workflow.tasks(id),
    primary key (task_id, depends_on_task_id),
    check (task_id <> depends_on_task_id)
);

create table workflow.approvals (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null references workflow.close_runs(id),
    package_hash text not null,
    snapshot_hash text not null,
    actor_subject text not null,
    decision text not null check (decision in ('approved', 'changes_requested')),
    comment text not null default '',
    decided_at timestamptz not null default now(),
    unique (run_id, package_hash, actor_subject, decided_at)
);

create table workflow.action_executions (
    id uuid primary key default gen_random_uuid(),
    organization_id text not null references workflow.organizations(id),
    run_id uuid not null references workflow.close_runs(id),
    approval_id uuid not null references workflow.approvals(id),
    provider text not null check (provider in ('xero', 'gmail')),
    operation text not null check (operation in ('create_draft_manual_journal', 'send_approved_request')),
    idempotency_key text not null unique,
    request_hash text not null,
    marker text not null,
    status text not null check (status in ('prepared', 'started', 'succeeded', 'failed', 'outcome_unknown', 'reconciled')),
    provider_request_id text,
    provider_object_id text,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now()
);

create table workflow.action_manifests (
    action_id uuid primary key references workflow.action_executions(id),
    run_id uuid not null references workflow.close_runs(id),
    package_hash text not null,
    proposal_hash text,
    request_hash text not null,
    provider_object_id text,
    status text not null,
    created_at timestamptz not null default now()
);

create table audit.events (
    id bigint generated always as identity primary key,
    organization_id text references workflow.organizations(id),
    run_id uuid references workflow.close_runs(id),
    event_type text not null,
    event_version integer not null default 1,
    payload_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table audit.provider_calls (
    id bigint generated always as identity primary key,
    organization_id text references workflow.organizations(id),
    run_id uuid references workflow.close_runs(id),
    provider text not null,
    operation text not null,
    request_id text,
    status text not null,
    request_hash text,
    response_hash text,
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table audit.webhook_receipts (
    provider text not null,
    provider_event_id text not null,
    signature_verified boolean not null,
    payload_hash text not null,
    received_at timestamptz not null default now(),
    payload_json jsonb not null,
    primary key (provider, provider_event_id)
);

create table audit.ai_calls (
    id bigint generated always as identity primary key,
    organization_id text references workflow.organizations(id),
    run_id uuid references workflow.close_runs(id),
    provider text not null default 'groq',
    model_id text not null,
    prompt_version text not null,
    schema_version text not null,
    input_hash text not null,
    output_hash text,
    validation_status text not null check (validation_status in ('verified', 'rejected')),
    latency_ms integer,
    input_tokens integer,
    output_tokens integer,
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table audit.policy_decisions (
    id bigint generated always as identity primary key,
    organization_id text references workflow.organizations(id),
    run_id uuid references workflow.close_runs(id),
    action_type text not null,
    decision text not null,
    reason text not null,
    policy_version text not null,
    created_at timestamptz not null default now()
);

create or replace function workflow.validate_organization_deployment()
returns trigger
language plpgsql
as $$
declare
    deployment workflow.deployments%rowtype;
begin
    if tg_op = 'UPDATE' and old.deployment_id is distinct from new.deployment_id then
        raise exception 'organization deployment cannot be changed';
    end if;
    select * into deployment from workflow.deployments where id = new.deployment_id;
    if not found then
        raise exception 'organization deployment does not exist';
    end if;
    if new.market <> deployment.market or new.functional_currency <> deployment.currency then
        raise exception 'organization market/currency does not match deployment';
    end if;
    return new;
end;
$$;

create trigger organizations_deployment_guard
before insert or update on workflow.organizations
for each row execute function workflow.validate_organization_deployment();

create or replace function workflow.validate_connection_deployment()
returns trigger
language plpgsql
as $$
declare
    deployment_mode text;
begin
    select d.mode into deployment_mode
    from workflow.organizations o
    join workflow.deployments d on d.id = o.deployment_id
    where o.id = new.organization_id;
    if not found then
        raise exception 'connection organization or deployment does not exist';
    end if;
    if deployment_mode = 'demo' and new.provider_environment not in ('demo', 'sandbox') then
        raise exception 'demo connection cannot use production environment';
    end if;
    if deployment_mode = 'production' and new.provider_environment <> 'production' then
        raise exception 'production connection cannot use demo or sandbox environment';
    end if;
    if new.provider = 'xero' and deployment_mode = 'demo' and new.provider_environment <> 'demo' then
        raise exception 'Xero demo connection must use the Demo Company environment';
    end if;
    return new;
end;
$$;

create trigger connections_deployment_guard
before insert or update on workflow.connections
for each row execute function workflow.validate_connection_deployment();

create or replace function workflow.reject_immutable_change()
returns trigger
language plpgsql
as $$
begin
    raise exception 'immutable AccountingOS record cannot be changed or deleted';
end;
$$;

create trigger immutable_raw_xero_demo
before update or delete on raw_xero_demo.records
for each row execute function workflow.reject_immutable_change();

create trigger immutable_raw_bank_demo
before update or delete on raw_bank_demo.records
for each row execute function workflow.reject_immutable_change();

create trigger immutable_normalized_versions
before update or delete on normalized.record_versions
for each row execute function workflow.reject_immutable_change();

create trigger immutable_snapshots
before update or delete on normalized.source_snapshots
for each row execute function workflow.reject_immutable_change();

create trigger immutable_snapshot_records
before update or delete on normalized.snapshot_records
for each row execute function workflow.reject_immutable_change();

create trigger immutable_action_manifests
before update or delete on workflow.action_manifests
for each row execute function workflow.reject_immutable_change();

alter table workflow.deployments enable row level security;
alter table workflow.organizations enable row level security;
alter table workflow.organization_users enable row level security;
alter table workflow.connections enable row level security;
alter table workflow.close_runs enable row level security;
alter table raw_xero_demo.records enable row level security;
alter table raw_bank_demo.records enable row level security;
alter table normalized.source_batches enable row level security;
alter table normalized.record_versions enable row level security;
alter table normalized.source_snapshots enable row level security;
alter table normalized.snapshot_records enable row level security;
alter table normalized.evidence_items enable row level security;
alter table workflow.tasks enable row level security;
alter table workflow.task_dependencies enable row level security;
alter table workflow.approvals enable row level security;
alter table workflow.action_executions enable row level security;
alter table workflow.action_manifests enable row level security;
alter table audit.events enable row level security;
alter table audit.provider_calls enable row level security;
alter table audit.webhook_receipts enable row level security;
alter table audit.ai_calls enable row level security;
alter table audit.policy_decisions enable row level security;

revoke all on all tables in schema workflow, raw_xero_demo, raw_bank_demo, normalized, audit
from anon, authenticated;
revoke all on all sequences in schema raw_xero_demo, raw_bank_demo, audit
from anon, authenticated;

alter default privileges in schema workflow, raw_xero_demo, raw_bank_demo, normalized, audit
revoke all on tables from anon, authenticated;
