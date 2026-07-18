# AccountingOS Delivery Plans

This directory turns the approved v1.3 specification into an execution sequence.
The plan is deliberately split by exit criteria, not by technical layer: each
phase produces a demonstrable capability while preserving the demo/live safety
boundary.

## Plan index

- [Phase-by-phase implementation plan](implementation-plan.md) — Phases 0–7,
  dependencies, work items, tests, release gates, and explicit exclusions.

## Current position

The repository has the first foundation slice:

- a Python domain core for deployment guards, snapshots, approvals, balanced
  journal proposals, and Xero draft reconciliation;
- a FastAPI shell for health and close-run state;
- a static Next.js demo shell;
- unit and API tests for the implemented safety rules.

This is **not** an integrated demo yet. Phase 0 proves external capabilities;
Phases 1–6 build the isolated synthetic demo; Phase 7 is a separately gated
live-product expansion.

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

