# Phases 3–7 Operator Runbook

This runbook records what the code now enforces and what still requires an
external provider or compliance sign-off. The local verification command is:

```sh
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

## Phase 3 — Evidence and controlled email

`EvidenceCollector` accepts only configured Drive folders, Gmail mailbox/labels,
and the selected date range. `evaluate_checklist` produces deterministic
satisfied/missing requirements. `GmailRequestService` permits only an approved
template and allowlisted recipient, forbids attachments and prohibited content,
and records a marker before sending. An unavailable or ambiguous Sent search
becomes `outcome_unknown`; it never resends automatically.

The injected clients in `backend/tests/test_phase3.py` must be replaced with
the scoped production Google Workspace client only after OAuth and mailbox
evidence is attached.

## Phase 4 — Reconciliation and reports

`reconcile` produces exact, date-window, aggregate, and explicit fee matches.
Pending transactions, duplicate candidates, and unmatched records remain
exceptions or policy exclusions. `compute_reports` fails closed on trial-balance,
accounting-equation, or cash-reconciliation differences. Journal proposals must
use current account codes and cite evidence on every line.

## Phase 5 — Grounded AI

`GroundedExplanationService` receives a bounded `ExplanationContext` and
validates every evidence ID, amount, account code, and date against it. Invalid
structured output is retried once, then rejected. Prompt-injection-like output
and unsupported facts never enter review. Audit records retain hashes and
validation metadata, not chain-of-thought.

## Phase 6 — Approval and Xero DRAFT

`ReviewPackage.freeze` hashes the snapshot and proposal set. `XeroPolicyGateway`
requires the configured controller and exact package hash, creates only `DRAFT`
manual journals, uses an `AOSMJv1/...` marker, and verifies exact read-back.
Timeouts or unavailable marker searches become `outcome_unknown`. The gateway
has no post, update, delete, void, or payment operation.

## Phase 7 — Live market gates

`ExpansionRegistry` requires separate production/live US and India resources.
US requires Xero production-source, Plaid Production, Google, B2, and Groq
evidence. India is deferred and separately requires Setu agreement, FIU
eligibility, supported FIP, and approved retention policy. Market artifacts
cannot cross boundaries.

These gates do not manufacture provider or legal evidence. A live release still
requires the inventories, external acceptance evidence, backups, retention,
security, accessibility, and compliance approvals listed in `docs/PRD.md` and
`docs/live_integrations.md`.
