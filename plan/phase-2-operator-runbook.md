# Phase 2 Operator Runbook

Phase 2 proves that a close run can read the isolated demo providers and commit
one reproducible immutable source snapshot. The adapters are worker-facing;
the browser never receives provider tokens or writes raw provider records.

## Local verification

From `backend/`, run:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The Phase 2 tests cover deterministic payload hashing, Xero demo pagination and
tenant checks, Plaid cursor changes (added/modified/removed), cursor recovery,
bounded reads, and atomic snapshot creation.

## Provider adapter contract

- `XeroDemoAdapter` (also exported as `XeroDirectDemoAdapter`) accepts an
  injected `XeroDemoClient`. It reads page 1 onward, requires the configured
  demo tenant and environment, rejects duplicate/out-of-order records, and
  stops at a configured page limit.
- `PlaidSandboxAdapter` accepts an injected `PlaidSandboxClient` and a
  `PlaidCursorState`. It stages all pages, applies changes and removals, then
  commits the new cursor only after every normalized version succeeds. A failed
  page leaves the previous cursor and records intact for a safe retry.
- `normalize_provider_record` stores a canonical JSON payload and SHA-256 hash,
  plus provider ID, observed timestamp, currency, and accounting date.

## Worker execution

`DemoIngestionService.synchronize` reads both adapters and calls the domain
snapshot boundary once. If either provider is incomplete, mismatched, or
fails, no snapshot is committed and the run becomes `blocked`; retrying starts
from the provider's last committed cursor.

The provider semantics remain testable with injected clients, while the Phase 8
Supabase migration and server-side repository now define the durable persistence
boundary. Wiring real provider credentials into those repositories is still a
later phase; do not substitute local fixtures for provider capability evidence.
