# System Architecture and Execution Model

**Status:** Superseded

This document previously described a broad future-state agent platform. Its
state machine, event-bus design, memory layers, and service boundaries are not
the AccountingOS MVP architecture.

Use these documents instead:

- `demo_architecture.md` for the isolated synthetic US demo boundary.
- `PRD.md` for the approved MVP product scope and acceptance criteria.
- `TDD.md` for the authoritative architecture, data contracts, workflow DAG,
  state transitions, security boundaries, test strategy, and implementation
  order.

Future platform architecture should be designed only after the MVP is tested
with controllers. Do not implement Redis, an event bus, distributed agents, or
long-term memory from the earlier version of this document.
