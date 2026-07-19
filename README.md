# AccountingOS

AccountingOS prepares a reviewable month-end close package from authenticated
production provider data. The active scope is US production; it never posts
journals, moves money, or locks periods. Synthetic-demo materials are retained
only as isolated test fixtures.

## Current implementation

The backend contains the foundations for the production product and its
separately isolated test fixtures:

- deployment, connection, source-batch, and immutable snapshot boundaries;
- Xero/Plaid ingestion contracts with cursor and pagination recovery;
- scoped Drive/Gmail evidence, checklist evaluation, and controlled email;
- deterministic reconciliation, exceptions, journals, and report invariants;
- bounded AI explanations with citation and amount validation;
- frozen controller packages and Xero `DRAFT`-only action gateway;
- US production release gates; India is deferred;
- Supabase Auth verification on every workflow API request, with organization
  membership and role checks performed server-side;
- a private, TLS-only Supabase Postgres workflow store for organizations,
  connections, idempotent close-run creation, and durable OAuth state;
- a durable, leased worker with persisted task events, safe retry/cancellation,
  atomic Xero/Plaid source snapshots, scoped Google evidence collection, and
  frozen review-package approval records;
- a real Next.js controller flow: Supabase magic-link sign-in, organization
  bootstrap, Xero authorization handoff, truthful connection
  status, idempotent close-run creation, task progress, events, retry,
  cancellation, and review approval.

Provider account evidence, the organization-specific bank-to-ledger mapping,
artifact storage, and live acceptance remain explicitly gated. The worker
blocks safely until those inputs are configured; it never invents financial
mapping or silently substitutes fixtures. See [the documentation map](docs/README.md),
[the technical design](docs/TDD.md), and the [phase-by-phase delivery plan](plan/README.md).

The checked-in API, worker, browser controller, and environment template now
default to the live US production boundary. Fixture adapters remain available
only for separately configured test stacks; a production worker rejects fixture
source environments and unapproved Plaid accounts before persistence.

The active implementation track is US production: Supabase Postgres for private
workflow persistence and Groq for bounded explanations. India is deferred.

Controller identity uses Supabase Auth. The browser receives only the Supabase
project URL and publishable key; FastAPI verifies every bearer token with
Supabase Auth and enforces `organization_users` membership before accepting a
workflow request. The browser never reads a private database schema or holds a
database URL, service-role key, or provider credential.

Phase 1 identity and connection primitives live in
[`backend/app/security.py`](backend/app/security.py),
[`backend/app/connections.py`](backend/app/connections.py), and the SQL migrations
under `supabase/migrations/` (the single schema source of truth; see
[`backend/migrations/README.md`](backend/migrations/README.md)).

Phase 2 ingestion primitives live in
[`backend/app/providers.py`](backend/app/providers.py),
[`backend/app/normalization.py`](backend/app/normalization.py), and
[`backend/app/ingestion.py`](backend/app/ingestion.py). The durable runner is
[`backend/app/worker_main.py`](backend/app/worker_main.py); provider account
wiring remains safely blocked until the real production credentials are supplied.

## Phase 0 capability check

Production onboarding uses real provider capability evidence and never accepts
placeholders as proof of a working connection. The synthetic scenario remains a
test fixture only; see the [operator runbooks](plan/README.md).

## Local development

The backend targets Python 3.12 and FastAPI. Run its test suite from the
backend directory with the project virtual environment:

```sh
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

First copy the environment templates and replace the `replace-with...` values:

```sh
cp backend/.env.example backend/.env
cp web/.env.local.example web/.env.local
```

The API requires the server-only `SUPABASE_DB_URL`, `SUPABASE_URL`,
`SUPABASE_PUBLISHABLE_KEY`, and `ACCOUNTINGOS_BOOTSTRAP_CONTROLLER_EMAIL`.
The browser needs only `NEXT_PUBLIC_SUPABASE_URL`,
`NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`, and `NEXT_PUBLIC_API_BASE_URL`.
Provider and artifact values in `backend/.env` remain server-only. Keep the
template's `secret://` references and supply the actual values through the
chosen secret store; do not add any credential to `web/.env.local`.

Apply the repository migrations to the remote Supabase project after supplying
your project reference and authenticating the CLI:

```sh
npx supabase link --project-ref <your-project-ref>
npx supabase db push
```

Start the API with the environment loaded:

```sh
cd backend
set -a; source .env; set +a
.venv/bin/python -m uvicorn app.main:app --reload
```

Run the durable worker in a second terminal after applying the migrations:

```sh
cd backend
set -a; source .env; set +a
.venv/bin/python -m app.worker_main
```

Then start the web controller at `http://localhost:3000`:

```sh
cd web
npm run dev
```

Configure the Supabase dashboard with `http://localhost:3000` as an allowed
redirect URL for magic links. Configure the Xero callback URL as
`http://localhost:8000/api/v1/connections/xero/callback`. Provider connections
and source/evidence work intentionally remain blocked rather than fabricated
until their production credentials and capability evidence are available. The
production worker also requires the approved Xero tenant and selected Plaid
account IDs to be configured.
Reconciliation remains blocked until an accountant supplies the organization's
selected bank accounts, Xero ledger source, and approved account mapping; those
choices cannot be inferred safely from credentials.
