# Phase 8 Supabase Postgres Runbook

Phase 8 adds the US persistence boundary without exposing financial tables to
the browser.

## Project setup

The repository now contains a Supabase CLI project under `supabase/` and the
official migration scaffold:

```sh
npx --yes supabase --version
npx --yes supabase migration list --local
```

The migrations create private `workflow`, `raw_xero`, `raw_bank_us`,
`raw_xero_demo`, `raw_bank_demo`, `normalized`, and `audit` schemas, enable RLS
on every table, revoke Data API access for `anon` and `authenticated`, and add
deployment/environment guards. `raw_xero` and `raw_bank_us` hold only live US
production records; the `*_demo` schemas are fixture-only.

The FastAPI backend uses `SUPABASE_DB_URL` server-side with TLS. Never place a
database URL, service-role key, or `GROQ_API_KEY` in a `NEXT_PUBLIC_*` variable.

## Remote project rollout

After you enter real values in `backend/.env` and `web/.env.local`, link and
apply the migrations to the intended US production project:

```sh
npx supabase link --project-ref <your-project-ref>
npx supabase db push
```

Review the migration against a disposable Supabase branch or local database
first, then run `db push` against the approved US production project. The
generated integrity migration adds durable request idempotency, connection
uniqueness, and database guards that prevent cross-organization/deployment
source and action writes.

The API validates Supabase users with the Auth `/user` endpoint using the
server-held publishable key, then checks `workflow.organization_users`. The
browser uses the same project's URL and publishable key only for Auth; it does
not call the Data API for financial data.

## Local verification

With Docker Desktop running:

```sh
npx --yes supabase db start --workdir .
npx --yes supabase db reset --workdir .
npx --yes supabase db lint --local --fail-on error --workdir .
```

The current environment has the CLI and migration files, but Docker Desktop's
daemon must be running before these database commands can execute.

Backend contract tests run without a database:

```sh
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

`backend/app/supabase_db.py` enforces TLS URLs, server-only configuration,
transaction commit/rollback, task claims using `FOR UPDATE SKIP LOCKED`, and
append-only source/snapshot/audit write boundaries.

## Supabase security checks

- Keep financial schemas out of `[api].schemas` in `supabase/config.toml`.
- If a future browser feature needs a Supabase Data API table, add explicit
  grants and organization-aware RLS policies; do not rely on `TO authenticated`
  alone.
- Keep the FastAPI authorization boundary while validating Supabase Auth JWTs;
  Realtime, if enabled later, must not bypass it.
