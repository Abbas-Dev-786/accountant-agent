-- Versioned organization configuration and browser-initiated provider onboarding.
-- Provider credentials remain in the secret manager; this database stores only
-- the accountant-approved configuration and opaque connection metadata.

create table workflow.close_mappings (
    id uuid primary key default gen_random_uuid(),
    organization_id text not null references workflow.organizations(id),
    version integer not null check (version > 0),
    status text not null check (status in ('active', 'superseded')),
    configuration_json jsonb not null,
    approved_by_subject text not null,
    created_at timestamptz not null default now(),
    superseded_at timestamptz,
    unique (organization_id, version)
);

create unique index close_mappings_one_active_per_organization
on workflow.close_mappings (organization_id)
where status = 'active';

alter table workflow.close_runs
add column mapping_id uuid references workflow.close_mappings(id);

create or replace function workflow.validate_close_run_mapping()
returns trigger
language plpgsql
as $$
declare
    mapping workflow.close_mappings%rowtype;
begin
    if new.mapping_id is null then
        return new;
    end if;
    select * into mapping from workflow.close_mappings where id = new.mapping_id;
    if not found or mapping.organization_id <> new.organization_id then
        raise exception 'close run mapping must belong to its organization';
    end if;
    if tg_op = 'UPDATE' and old.mapping_id is not null and old.mapping_id is distinct from new.mapping_id then
        raise exception 'a close run mapping cannot change after creation';
    end if;
    return new;
end;
$$;

create trigger close_runs_mapping_guard
before insert or update on workflow.close_runs
for each row execute function workflow.validate_close_run_mapping();

alter table workflow.close_mappings enable row level security;
revoke all on workflow.close_mappings from anon, authenticated;

alter default privileges in schema workflow
revoke all on tables from anon, authenticated;
