# AccountingOS

AccountingOS prepares a reviewable month-end close package from authenticated
provider data. The first build is an isolated US synthetic-data demo; it never
posts journals, moves money, or locks periods.

## Current implementation

The initial foundation implements the safety-critical domain rules before any
provider credential is accepted:

- immutable demo/production deployment guards;
- immutable source-batch snapshots;
- close-run state transitions and controller approval;
- balanced journal proposals;
- deterministic Xero draft markers, read-back comparison, and unknown-outcome
  recovery.

The provider adapters are deliberately not live yet. See [the documentation
map](docs/README.md), [the technical design](docs/TDD.md), and the
[phase-by-phase delivery plan](plan/README.md).

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
