-- Database invariants for the server-owned AccountingOS workflow.  The API
-- connects as a private server role, but these guards make cross-organization
-- or cross-deployment writes invalid even if a future repository regresses.

alter table workflow.close_runs
    add column request_key text;

alter table workflow.close_runs
    add constraint close_runs_organization_request_key_unique
    unique (organization_id, request_key);

alter table workflow.connections
    add constraint connections_organization_provider_tenant_unique
    unique (organization_id, provider, provider_tenant_or_account_id);

alter table raw_xero.records
    add constraint raw_xero_source_batch_fk
    foreign key (source_batch_id) references normalized.source_batches(id);

alter table raw_bank_us.records
    add constraint raw_bank_us_source_batch_fk
    foreign key (source_batch_id) references normalized.source_batches(id);

alter table raw_xero_demo.records
    add constraint raw_xero_demo_source_batch_fk
    foreign key (source_batch_id) references normalized.source_batches(id);

alter table raw_bank_demo.records
    add constraint raw_bank_demo_source_batch_fk
    foreign key (source_batch_id) references normalized.source_batches(id);

create or replace function workflow.validate_close_run_deployment()
returns trigger
language plpgsql
as $$
declare
    deployment workflow.deployments%rowtype;
    organization workflow.organizations%rowtype;
begin
    select * into organization from workflow.organizations where id = new.organization_id;
    if not found then
        raise exception 'close run organization does not exist';
    end if;
    if organization.deployment_id <> new.deployment_id then
        raise exception 'close run deployment does not match its organization';
    end if;
    select * into deployment from workflow.deployments where id = new.deployment_id;
    if not found then
        raise exception 'close run deployment does not exist';
    end if;
    if new.deployment_mode <> deployment.mode
       or new.data_class <> deployment.data_class
       or new.market <> deployment.market
       or new.currency <> deployment.currency then
        raise exception 'close run values do not match immutable deployment configuration';
    end if;
    if tg_op = 'UPDATE' and (
        old.organization_id is distinct from new.organization_id
        or old.deployment_id is distinct from new.deployment_id
        or old.deployment_mode is distinct from new.deployment_mode
        or old.data_class is distinct from new.data_class
        or old.market is distinct from new.market
        or old.currency is distinct from new.currency
    ) then
        raise exception 'close run deployment boundary cannot be changed';
    end if;
    return new;
end;
$$;

create trigger close_runs_deployment_guard
before insert or update on workflow.close_runs
for each row execute function workflow.validate_close_run_deployment();

create or replace function normalized.validate_source_batch_run()
returns trigger
language plpgsql
as $$
declare
    run_organization_id text;
begin
    select organization_id into run_organization_id from workflow.close_runs where id = new.run_id;
    if not found or run_organization_id <> new.organization_id then
        raise exception 'source batch must belong to its close run organization';
    end if;
    return new;
end;
$$;

create trigger source_batches_run_guard
before insert or update on normalized.source_batches
for each row execute function normalized.validate_source_batch_run();

create or replace function normalized.validate_source_batch_deployment()
returns trigger
language plpgsql
as $$
declare
    run_mode text;
    run_data_class text;
begin
    select deployment_mode, data_class into run_mode, run_data_class
    from workflow.close_runs where id = new.run_id;
    if not found then
        raise exception 'source batch close run does not exist';
    end if;
    if (run_mode, run_data_class) = ('production', 'live')
       and new.provider_environment <> 'production' then
        raise exception 'live close runs require production source batches';
    end if;
    if (run_mode, run_data_class) = ('demo', 'synthetic')
       and ((new.provider = 'xero' and new.provider_environment <> 'demo')
            or (new.provider = 'plaid' and new.provider_environment <> 'sandbox')) then
        raise exception 'fixture close runs require fixture source batches';
    end if;
    return new;
end;
$$;

create trigger source_batches_deployment_guard
before insert or update on normalized.source_batches
for each row execute function normalized.validate_source_batch_deployment();

create or replace function workflow.validate_raw_source_record()
returns trigger
language plpgsql
as $$
declare
    batch_organization_id text;
    batch_run_id uuid;
begin
    select organization_id, run_id into batch_organization_id, batch_run_id
    from normalized.source_batches where id = new.source_batch_id;
    if not found or new.organization_id <> batch_organization_id or new.run_id <> batch_run_id then
        raise exception 'raw source record must match its source batch organization and close run';
    end if;
    return new;
end;
$$;

create trigger raw_xero_context_guard
before insert on raw_xero.records
for each row execute function workflow.validate_raw_source_record();

create trigger raw_bank_us_context_guard
before insert on raw_bank_us.records
for each row execute function workflow.validate_raw_source_record();

create trigger raw_xero_demo_context_guard
before insert on raw_xero_demo.records
for each row execute function workflow.validate_raw_source_record();

create trigger raw_bank_demo_context_guard
before insert on raw_bank_demo.records
for each row execute function workflow.validate_raw_source_record();

create or replace function normalized.validate_snapshot_run()
returns trigger
language plpgsql
as $$
declare
    run workflow.close_runs%rowtype;
begin
    select * into run from workflow.close_runs where id = new.run_id;
    if not found then
        raise exception 'snapshot close run does not exist';
    end if;
    if new.organization_id <> run.organization_id
       or new.deployment_id <> run.deployment_id
       or new.deployment_mode <> run.deployment_mode
       or new.data_class <> run.data_class then
        raise exception 'snapshot must match its close run boundary';
    end if;
    return new;
end;
$$;

create trigger snapshots_run_guard
before insert on normalized.source_snapshots
for each row execute function normalized.validate_snapshot_run();

create or replace function workflow.validate_task_dependency_run()
returns trigger
language plpgsql
as $$
declare
    task_run_id uuid;
    dependency_run_id uuid;
begin
    select run_id into task_run_id from workflow.tasks where id = new.task_id;
    select run_id into dependency_run_id from workflow.tasks where id = new.depends_on_task_id;
    if task_run_id is null or dependency_run_id is null or task_run_id <> dependency_run_id then
        raise exception 'workflow task dependencies must stay within one close run';
    end if;
    return new;
end;
$$;

create trigger task_dependencies_run_guard
before insert or update on workflow.task_dependencies
for each row execute function workflow.validate_task_dependency_run();

create or replace function workflow.validate_action_execution_context()
returns trigger
language plpgsql
as $$
declare
    approval_run_id uuid;
    run_organization_id text;
begin
    select run_id into approval_run_id from workflow.approvals where id = new.approval_id;
    select organization_id into run_organization_id from workflow.close_runs where id = new.run_id;
    if approval_run_id is null or approval_run_id <> new.run_id or run_organization_id <> new.organization_id then
        raise exception 'action execution must match its approval, close run, and organization';
    end if;
    return new;
end;
$$;

create trigger action_executions_context_guard
before insert or update on workflow.action_executions
for each row execute function workflow.validate_action_execution_context();

alter table workflow.close_runs enable row level security;
alter table raw_xero.records enable row level security;
alter table raw_bank_us.records enable row level security;
alter table raw_xero_demo.records enable row level security;
alter table raw_bank_demo.records enable row level security;

revoke all on workflow.close_runs, raw_xero.records, raw_bank_us.records, raw_xero_demo.records, raw_bank_demo.records
from anon, authenticated;
