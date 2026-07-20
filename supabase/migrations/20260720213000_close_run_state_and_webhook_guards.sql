-- Preserve terminal close records even if an application bug bypasses the
-- repository guard.  Retrying is deliberately limited to operationally
-- blocked/failed runs; an approved package or cancelled run is immutable.

create or replace function workflow.guard_close_run_state_transition()
returns trigger
language plpgsql
as $$
begin
    if old.state in ('approved', 'cancelled') and new.state <> old.state then
        raise exception 'terminal close runs cannot change state';
    end if;
    if new.state = 'synchronizing' and old.state not in ('blocked', 'failed', 'synchronizing') then
        raise exception 'only blocked or failed close runs may resume synchronization';
    end if;
    return new;
end;
$$;

drop trigger if exists close_runs_state_transition_guard on workflow.close_runs;
create trigger close_runs_state_transition_guard
before update of state on workflow.close_runs
for each row execute function workflow.guard_close_run_state_transition();

-- Webhook data is server-only audit evidence.  The API's conflict check also
-- rejects a reused event identifier whose canonical payload differs.
revoke all on audit.webhook_receipts from anon, authenticated;
