# AccountingOS Documentation Map

AccountingOS is a production-first US close-readiness product for Xero
organizations. Its isolated synthetic environment is a test fixture, not a
product milestone or a substitute for live acceptance.

## Authoritative for the Production Product

Read these documents in order:

1. `PRD.md` - product outcome, production data boundaries, action policy, scope,
   and acceptance criteria.
2. `live_integrations.md` - production provider onboarding, regional
   requirements, runbooks, and go-live gates.
3. `TDD.md` - architecture, adapters, synchronization, snapshots, workflow,
   policy, controls, persistence, security, testing, and implementation order.
4. `user_story.md` - production controller narrative and user-visible behavior.
5. `production_onboarding.md` - organization setup, accounting mapping, provider
   evidence, and the US production release gate.
6. `demo_architecture.md` - isolated test-fixture boundary, scenario bootstrap,
   and non-production test controls.

## Supporting Strategy

- `vision_product_strategy.md` describes the long-term product thesis. Xero is
  the only MVP accounting system even where the strategy mentions other ERPs.
- `workflow_bible.md` describes the broader real-world month-end close. It does
  not add MVP requirements.
- `agent_bible.md` provides future-state domain language. MVP agent names are
  logical task owners behind owned, policy-controlled MCP tools.

## Superseded

- `system_architecture.md` is retained only as a pointer to `TDD.md`.
- The demo-first v1.3 direction was superseded on 2026-07-19 by the
  production-first US scope. Demo documents describe fixtures only.

## Conflict Rule

1. `PRD.md` controls product behavior and user-visible claims.
2. `live_integrations.md` controls provider behavior and production-access requirements.
3. `TDD.md` controls implementation behavior.
4. `user_story.md` may simplify presentation but may not expand behavior.
5. `demo_architecture.md` controls fixture isolation only.
6. Strategy, workflow, and agent documents cannot add MVP requirements.

The root `../contradictions.md` records the review history and final decisions.

## Current runnable path

The current application path is Supabase magic-link sign-in → server-side
Supabase token validation → controller bootstrap → Xero OAuth handoff →
idempotent close-run creation. Browser requests go only to FastAPI; all
financial tables live in private Supabase schemas. See the root
[`README.md`](../README.md) for the required environment values and migration
commands.

The provider adapters and accounting controls are implemented and tested, but a
production close run remains blocked until the live providers, server-side secret
references, organization mapping, and worker have been configured. This is
deliberate: the UI must not label a source healthy or manufacture a review
package.
