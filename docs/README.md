# AccountingOS Documentation Map

AccountingOS is a close-readiness product with an isolated US synthetic-data
demo milestone and a later live product for Xero organizations.

## Authoritative for the Demo MVP and Live Product

Read these documents in order:

1. `demo_architecture.md` - isolated demo environment, provider boundaries,
   scenario bootstrap/baseline verification, and demo acceptance rules.
2. `PRD.md` - product outcome, demo/live data boundaries, action policy, scope,
   and acceptance criteria.
3. `live_integrations.md` - demo provider setup plus later production/provider
   onboarding, regional requirements, runbooks, and go-live gates.
4. `TDD.md` - architecture, adapters, synchronization, snapshots, workflow,
   policy, controls, persistence, security, testing, and implementation order.
5. `user_story.md` - truthful synthetic demo narrative and later live behavior.

## Supporting Strategy

- `vision_product_strategy.md` describes the long-term product thesis. Xero is
  the only MVP accounting system even where the strategy mentions other ERPs.
- `workflow_bible.md` describes the broader real-world month-end close. It does
  not add MVP requirements.
- `agent_bible.md` provides future-state domain language. MVP agent names are
  logical task owners behind owned, policy-controlled MCP tools.

## Superseded

- `system_architecture.md` is retained only as a pointer to `TDD.md`.
- The fixture-only v1.0 direction was superseded by the isolated synthetic demo
  plus separately gated live product defined in the v1.2 documents.

## Conflict Rule

1. `demo_architecture.md` controls demo environment and data-boundary claims.
2. `PRD.md` controls product behavior and user-visible claims.
3. `live_integrations.md` controls provider behavior and production-access requirements.
4. `TDD.md` controls implementation behavior.
5. `user_story.md` may simplify presentation but may not expand behavior.
6. Strategy, workflow, and agent documents cannot add MVP requirements.

The root `../contradictions.md` records the review history and final decisions.
