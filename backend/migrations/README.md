# Backend migrations

The single source of truth for the database schema lives in
[`supabase/migrations/`](../../supabase/migrations/), applied with the Supabase
CLI (`supabase db reset` / `supabase migration up`).

The previous `0001_phase1_boundaries.sql` in this directory was a divergent,
partial fork (only ~4 of the 22 tables, with conflicting `organizations.id` and
`connections.provider` definitions). It was retired to avoid two conflicting
schema definitions. `backend/app/supabase_db.py` targets the Supabase schema
(`workflow.close_runs`, `audit.events`, `normalized.*`), so that migration set is
authoritative.

Do not reintroduce raw SQL files here. Add new schema changes as timestamped
migrations under `supabase/migrations/`.
