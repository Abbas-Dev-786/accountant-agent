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
  atomic Xero/Plaid source snapshots, scoped Google evidence collection,
  persisted reconciliation/results/reports, grounded Groq explanation audit,
  B2 compliance-retained close packages, and frozen review-package approval
  records;
- worker-only Gmail recovery drafts/sends and worker-only Xero manual-journal
  `DRAFT` creation with marker recovery and exact read-back;
- a real Next.js controller flow: Supabase magic-link sign-in, organization
  bootstrap, Xero authorization, Plaid Link bank consent, Google Workspace
  OAuth, versioned accountant-approved mapping, truthful connection status,
  idempotent close-run creation, SSE progress replay, source/evidence/reconciliation/
  exception/report/artifact review, worker-action recovery, cancellation, and
  review approval.

Remote migration application, production credentials, and live acceptance
remain explicitly gated. The worker blocks safely until those inputs are
configured; it never invents financial mapping or silently substitutes
fixtures. See [the documentation map](docs/README.md),
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

On Windows PowerShell, use the equivalent virtual-environment path:

```powershell
cd backend
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

First copy the environment templates and replace the `replace-with...` values:

```sh
cp backend/.env.example backend/.env
cp web/.env.local.example web/.env.local
```

The API requires the server-only `SUPABASE_DB_URL`, `SUPABASE_URL`,
`SUPABASE_PUBLISHABLE_KEY`, and `ACCOUNTINGOS_BOOTSTRAP_CONTROLLER_EMAIL`.
Before starting the API or worker, provision every `secret://` reference from
`backend/.env.example` in Supabase Vault using that exact reference as the
Vault secret name. Provider onboarding then creates and rotates its connection
credentials in Vault; `workflow.connections` stores references only, never
token material.
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

```powershell
cd backend
Get-Content .env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process') } }
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Run the durable worker in a second terminal after applying the migrations:

```sh
cd backend
set -a; source .env; set +a
.venv/bin/python -m app.worker_main
```

```powershell
cd backend
Get-Content .env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process') } }
.venv\Scripts\python.exe -m app.worker_main
```

Before a production controller connects providers, run the read-only preflight.
It validates the production boundary, private Supabase schema, Vault references,
and obsolete file-secret settings without printing a credential or making a
provider write:

```sh
cd backend
.venv/bin/python -m app.production_preflight --env-file .env
```

```powershell
cd backend
.venv\Scripts\python.exe -m app.production_preflight --env-file .env
```

Then start the web controller at `http://localhost:3000`:

```sh
cd web
npm run dev
```

For production, configure Supabase Auth, Xero, Google, and Plaid with the exact
HTTPS web origin, callback URLs, and webhook URL from `backend/.env`; do not use
the local development URLs shown in older operator notes.

After a controller signs in, they connect the approved Xero tenant, complete
Plaid Link for the selected bank accounts, and authorize the configured Google
Workspace scopes. The controller then creates a versioned mapping that selects
the Xero tenant, maps each connected Plaid account to its Xero ledger account,
sets reconciliation tolerances, defines the evidence scope, and restricts
journal account codes. These selections are stored with the close run and are
never inferred from credentials. Provider work blocks with an actionable state
until the required production credentials, connection evidence, and mapping are
present.
