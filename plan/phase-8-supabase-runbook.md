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

The migration creates private `workflow`, `raw_xero_demo`, `raw_bank_demo`,
`normalized`, and `audit` schemas, enables RLS on every table, revokes Data API
access for `anon` and `authenticated`, and adds deployment/environment guards.

The FastAPI backend uses `SUPABASE_DB_URL` server-side with TLS. Never place a
database URL, service-role key, or `GROQ_API_KEY` in a `NEXT_PUBLIC_*` variable.

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
- Keep the existing FastAPI authorization boundary even if Supabase Auth or
  Realtime is enabled later.
