# DEBUG REPORT — AccountingOS production-flow readiness

- **Symptom:** A live close could not reconcile Xero cash activity, realistic evidence folders/mailboxes stopped the run, expired worker leases were orphaned, terminal runs could be retried, and the UI lost runs after reload. The report also identified provider, API-control, webhook, performance, and configuration gaps.
- **Root cause:** Production ingestion inherited the demo Invoices resource while reconciliation intentionally excludes invoices. Evidence adapters assumed one result page and treated benign out-of-period/unlabelled data as a policy failure. The task claimer selected only `ready` work, retry had no state guard, and streaming repeatedly performed synchronous per-client database calls. Several controls existed in worker-only code but were bypassed by API paths.
- **Fix:**
  - Xero production ingestion now reads BankTransactions, Payments, and ManualJournals; cash projections accept native Xero identifiers and nested bank-account codes.
  - Drive/Gmail use pagination; Gmail resolves configured label names to IDs and includes period-end; the collector filters benign non-period/non-label results.
  - Expired `running` leases are reclaimable, retries are limited to blocked/failed runs, and a database trigger protects approved/cancelled transitions.
  - SSE moves sync database work to threads, backs off, and terminates at stable states; the UI reconnects, debounces refreshes, and restores/list-selects historic runs.
  - API proposals and execution-time Xero actions enforce frozen permitted account codes. OAuth references are per authorization, connections can be disconnected, and Plaid Item IDs are persisted correctly.
  - Added bounded Postgres pooling, short-lived positive Supabase Auth caching, worker-loop exception handling, real Plaid signed-webhook verification, configuration/preflight guards, and Windows documentation.
- **Evidence:** `backend/.venv/bin/python -m unittest discover -s tests -v` passed (165 tests). `npm run build` passed in `web`.
- **Regression tests:** `backend/tests/test_provider_runtime.py`, `backend/tests/test_close_execution.py`, `backend/tests/test_phase3.py`, `backend/tests/test_supabase.py`, `backend/tests/test_api.py`, `backend/tests/test_plaid_webhooks.py`, `backend/tests/test_supabase_auth.py`, and OAuth/preflight tests cover the newly fixed paths.
- **Related:** A real-provider staging close is still required for credentials, tenant access, and provider-specific data shapes; the code now exposes failures safely rather than masking them.
- **Status:** DONE_WITH_CONCERNS — implementation and automated verification are complete; no live provider credentials were used in this workspace.
