# AccountingOS Delivery Plans

This directory turns the approved v1.3 specification into an execution sequence.
The plan is deliberately split by exit criteria, not by technical layer: each
phase produces a demonstrable capability while preserving the demo/live safety
boundary.

## Plan index

- [Phase-by-phase implementation plan](implementation-plan.md) — Phases 0–7,
  dependencies, work items, tests, release gates, and explicit exclusions.
- [Remaining work plan](remaining-work-plan.md) — persistence, real demo
  integrations, worker/API/UI completion, Groq, Supabase, and the US-only
  release path.
- [Phase 8 Supabase runbook](phase-8-supabase-runbook.md) — migration, local
  verification, and server-side security checks.
- [Phase 9 US/Groq runbook](phase-9-us-groq-runbook.md) — server-only demo
  provider adapters, structured AI output, and external evidence gates.
- [Phase 10 worker runbook](phase-10-worker-runbook.md) — task leases, bounded
  retries, cancellation, event replay, and webhook replay protection.
- [Phases 3–7 operator runbook](phase-3-7-operator-runbook.md) — implemented
  safety boundaries and remaining external release evidence.

## Current position

The repository has implemented foundations for Phases 0–7 and Phase 8–10 code
boundaries, with 92 backend tests and a successful Next.js production build.
The provider and compliance
gates that require external credentials, PostgreSQL/B2 infrastructure, or
market sign-off remain explicitly open; no mocked result is treated as live
acceptance.

## Operating rules for every phase

1. Keep demo and production credentials, databases, buckets, callbacks, and
   artifacts physically separate.
2. Treat a provider failure, stale source, partial delivery, or ambiguous action
   result as a visible product state. Never use a local fallback.
3. Do not add a payment, posting, delete, void, or period-lock API/tool.
4. Do not advance a phase on a mocked acceptance claim. Record the command,
   provider evidence, test result, or controller sign-off that proves its exit
   criterion.
5. Start Phase 7 only after a real pilot organization and every applicable
   external provider/compliance gate are available.

## How to use the plan

For a phase, create a small execution issue for each work item, assign an owner,
and attach the listed verification evidence before marking the phase complete.
If a prerequisite is not available, keep the phase blocked and continue only
with independent work from a later phase; do not replace it with a fake provider
result.
