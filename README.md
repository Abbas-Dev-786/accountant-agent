# AccountingOS

AccountingOS prepares a reviewable month-end close package from authenticated
provider data. The first build is an isolated US synthetic-data demo; it never
posts journals, moves money, or locks periods.

## Current implementation

The backend now contains the phase foundations for the isolated demo and the
separately gated live expansion:

- deployment, connection, source-batch, and immutable snapshot boundaries;
- Xero/Plaid ingestion contracts with cursor and pagination recovery;
- scoped Drive/Gmail evidence, checklist evaluation, and controlled email;
- deterministic reconciliation, exceptions, journals, and report invariants;
- bounded AI explanations with citation and amount validation;
- frozen controller packages and Xero `DRAFT`-only action gateway;
- separate production release gates (US is active; India is deferred).

Provider OAuth/API wiring, PostgreSQL persistence, and external compliance/provider
evidence remain explicitly gated. See [the documentation map](docs/README.md),
[the technical design](docs/TDD.md), and the [phase-by-phase delivery
plan](plan/README.md).

The next implementation track uses Supabase Postgres for persistence and Groq
for bounded agent explanations. The active product scope is the isolated US
demo followed by a US production pilot; India is deferred.

Phase 1 identity and connection primitives live in
[`backend/app/security.py`](backend/app/security.py),
[`backend/app/connections.py`](backend/app/connections.py), and the SQL migrations
under `supabase/migrations/` (the single schema source of truth; see
[`backend/migrations/README.md`](backend/migrations/README.md)).

Phase 2 ingestion primitives live in
[`backend/app/providers.py`](backend/app/providers.py),
[`backend/app/normalization.py`](backend/app/normalization.py), and
[`backend/app/ingestion.py`](backend/app/ingestion.py). They use injected demo
clients for deterministic tests; live OAuth/API wiring and PostgreSQL raw-table
persistence remain explicitly gated follow-up work.

## Phase 0 capability check

Phase 0 uses a versioned scenario plus actual operator-collected provider
evidence. See the [operator runbook](plan/phase-0-operator-runbook.md). The
verifier intentionally does not make provider calls or accept placeholders as a
successful demo setup.

## Local development

The backend targets Python 3.12 and FastAPI. The pure domain test suite can run
without third-party packages:

```sh
python3 -m unittest discover -s backend/tests -v
```

After installing the backend dependencies, start the API with:

```sh
uvicorn app.main:app --app-dir backend --reload
```

The web app is a Next.js shell in `web/`. Its environment is configured through
`NEXT_PUBLIC_API_BASE_URL`; it must point only to the isolated demo API during
the demo milestone.
