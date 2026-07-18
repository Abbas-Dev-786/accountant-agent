-- AccountingOS Phase 1: deployment, organization, connection, and audit boundaries.
-- Provider secrets never belong in this database. Store only secret:// references.

create extension if not exists pgcrypto;

create schema if not exists workflow;
create schema if not exists audit;

create table if not exists workflow.deployments (
    id text primary key,
    mode text not null check (mode in ('demo', 'production')),
    data_class text not null check (data_class in ('synthetic', 'live')),
    market text not null check (market in ('US', 'IN')),
    functional_currency text not null check (functional_currency in ('USD', 'INR')),
    created_at timestamptz not null default now(),
    check ((mode = 'demo' and data_class = 'synthetic' and market = 'US' and functional_currency = 'USD')
        or (mode = 'production' and data_class = 'live'))
);

create table if not exists workflow.organizations (
    id uuid primary key default gen_random_uuid(),
    deployment_id text not null references workflow.deployments(id),
    name text not null check (length(trim(name)) between 1 and 200),
    market text not null check (market in ('US', 'IN')),
    functional_currency text not null check (functional_currency in ('USD', 'INR')),
    accounting_timezone text not null,
    status text not null default 'active' check (status in ('active', 'disconnected', 'suspended')),
    created_at timestamptz not null default now(),
    check ((market = 'US' and functional_currency = 'USD') or (market = 'IN' and functional_currency = 'INR'))
);

create or replace function workflow.validate_organization_deployment()
returns trigger language plpgsql as $$
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
    if new.market <> deployment.market or new.functional_currency <> deployment.functional_currency then
        raise exception 'organization market/currency does not match deployment';
    end if;
    return new;
end;
$$;

drop trigger if exists organizations_deployment_guard on workflow.organizations;
create trigger organizations_deployment_guard
before insert or update on workflow.organizations
for each row execute function workflow.validate_organization_deployment();

create table if not exists workflow.organization_users (
    organization_id uuid not null references workflow.organizations(id),
    user_id uuid not null,
    identity_issuer text not null,
    identity_subject text not null,
    role text not null check (role in ('controller')),
    created_at timestamptz not null default now(),
    primary key (organization_id, user_id),
    unique (identity_issuer, identity_subject, organization_id)
);

create table if not exists workflow.connections (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references workflow.organizations(id),
    provider text not null check (provider in ('xero', 'plaid', 'google_drive', 'gmail', 'b2', 'openai', 'oidc')),
    provider_environment text not null check (provider_environment in ('sandbox', 'demo', 'production')),
    provider_tenant_or_account_id text not null,
    credential_secret_ref text not null check (credential_secret_ref like 'secret://%'),
    status text not null default 'connecting' check (status in ('connecting', 'healthy', 'delayed', 'partial', 'expired', 'revoked', 'failed', 'disconnected')),
    granted_scopes jsonb not null default '[]'::jsonb,
    last_verified_at timestamptz,
    last_success_at timestamptz,
    consent_expires_at timestamptz,
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique (organization_id, provider)
);

create or replace function workflow.validate_connection_deployment()
returns trigger language plpgsql as $$
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

drop trigger if exists connections_deployment_guard on workflow.connections;
create trigger connections_deployment_guard
before insert or update on workflow.connections
for each row execute function workflow.validate_connection_deployment();

create table if not exists workflow.close_configuration_versions (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references workflow.organizations(id),
    version integer not null check (version > 0),
    configuration_json jsonb not null,
    created_by uuid not null,
    created_at timestamptz not null default now(),
    superseded_at timestamptz,
    unique (organization_id, version)
);

create sequence if not exists audit.audit_event_sequence;

create table if not exists audit.audit_events (
    sequence bigint primary key default nextval('audit.audit_event_sequence'),
    event_id uuid not null unique default gen_random_uuid(),
    deployment_id text not null references workflow.deployments(id),
    organization_id uuid references workflow.organizations(id),
    actor_subject text,
    event_type text not null,
    provider text,
    request_id text,
    action_id text,
    payload_hash text,
    metadata_json jsonb not null default '{}'::jsonb,
    occurred_at timestamptz not null default now()
);

create index if not exists organization_users_identity_idx
    on workflow.organization_users(identity_issuer, identity_subject);
create index if not exists connections_org_status_idx
    on workflow.connections(organization_id, status);
create index if not exists audit_events_org_sequence_idx
    on audit.audit_events(organization_id, sequence);
