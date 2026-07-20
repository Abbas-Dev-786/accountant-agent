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
- **Evidence:** `backend/.venv/bin/python -m unittest discover -s tests -v` passed (172 tests). `npm run build` passed in `web`.
- **Regression tests:** `backend/tests/test_provider_runtime.py`, `backend/tests/test_close_execution.py`, `backend/tests/test_phase3.py`, `backend/tests/test_supabase.py`, `backend/tests/test_api.py`, `backend/tests/test_plaid_webhooks.py`, `backend/tests/test_supabase_auth.py`, and OAuth/preflight tests cover the newly fixed paths.
- **Related:** A real-provider staging close is still required for credentials, tenant access, and provider-specific data shapes; the code now exposes failures safely rather than masking them.
- **Status:** DONE_WITH_CONCERNS — implementation and automated verification are complete; no live provider credentials were used in this workspace.

## Follow-up DEBUG REPORT — real-provider semantics and recovery safety

- **Symptom:** The initial ingestion fix fetched real Xero cash resources but still treated unsigned Xero amounts as Plaid-signed amounts, parsed Xero wire dates as ISO, and rejected/blocked valid Payment and ManualJournal shapes. Expired task leases could be reclaimed without a limit. The terminal-run state trigger also caused action/email failures to roll back when they tried to mutate an approved close.
- **Root cause:** The cash projection lacked a provider-specific interpretation layer. The SQL queue persisted an attempt counter but no retry budget or exhaustion transition. Generic failure paths did not distinguish a close workflow from post-approval recovery work.
- **Fix:**
  - Normalization now accepts Xero `/Date(milliseconds±offset)/` values and derives a currency from supported nested Xero records without ever interpreting `CurrencyRate` as an ISO code. Production ingestion also freezes the already-authorized Xero Accounts metadata so Payments and ManualJournal lines can use authoritative account currency/type when their wire payload omits it.
  - Cash matching maps Xero `SPEND` to Plaid-positive outflow and `RECEIVE` to Plaid-negative inflow. Payments use `BankAmount` and `PaymentType`; unclassified direction is deliberately left unmatched. Manual Journals lacking account types no longer abort cash reconciliation; financial reports are explicitly marked unavailable pending authoritative account classification.
  - `workflow.tasks` now has a persisted `max_attempts` (default 3, constrained to 1–10). Lease-expired tasks at the limit become `failed`, emit `task_attempts_exhausted`, and block only nonterminal close runs. A controller retry resets the counter.
  - Action failure/outcome-unknown and task-blocked transitions no longer mutate approved or cancelled closes, so a recovery-email failure leaves an auditable action/task outcome rather than rolling back and being reclaimed forever.
  - Google evidence policy/provider errors are converted to a visible `TaskBlocked` result; expected out-of-period/unlabelled evidence remains filtered.
- **Regression coverage:** Xero wire date/sign/Payment/ManualJournal fixtures, normalization, exhausted leases, retry reset, approved-run action/task failures, and evidence provider failures.
- **Evidence:** Full backend suite passed: `172 tests` via `backend/.venv/bin/python -m unittest discover -s tests -v`.
- **Remaining validation:** Run one controlled staging close with an actual Xero tenant (including a multicurrency Payment) and real Google scopes before production activation.

## Follow-up DEBUG REPORT — linked Vault migration

- **Symptom:** `npx supabase db reset --linked` stopped in `20260720121500_secure_supabase_vault_permissions.sql` with `permission denied for function _crypto_aead_det_noncegen` while revoking every function in the Vault schema.
- **Root cause:** Vault includes extension-owned cryptographic helper functions. The linked-project migration role cannot modify their ACLs, so the blanket `REVOKE ALL ON ALL FUNCTIONS IN SCHEMA vault` aborts the whole migration.
- **Fix:** Removed the unsupported bulk function ACL and ineffective default ACLs. The migration still revokes `USAGE` on the `vault` schema plus every table/view and sequence from `PUBLIC`, `anon`, and `authenticated`; this blocks those roles from resolving Vault functions and reading `vault.decrypted_secrets` without trying to mutate extension internals.
- **Evidence:** `backend/.venv/bin/python -m unittest tests.test_supabase -v` passed (19 tests), including the Vault migration regression assertion; `git diff --check` passed.
- **Next action:** Re-run `npx supabase db reset --linked`. Do not use a direct grant on the Vault helper function as a workaround.
