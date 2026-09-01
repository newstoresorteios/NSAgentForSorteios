# Agent changelog v58–v61

Brief release notes for `openai-db-context-multichannel-runtime-v58` through **v61**.

## v58 — Sticky brand scope + dialogue phase in prompt

- Fixed sticky-brand detection in the scope send-gate (Baltic/Hamilton/Certina regressions).
- Exposed `dialogue_phase` in the compiled prompt so the model knows discovery vs shortlist vs buy.
- Added structural tests for dialogue phase transitions and scope-gate blocking/retry.

## v59 — Explicit-no memory + intent router

- Contact preference memory now honors `explicit_no` over remembered brand/style slots.
- New lightweight intent router (`app/sales/intent_router.py`) to steer commerce vs institutional paths before heavy sales logic.
- Memory policy helpers and unit tests for explicit-no precedence.

## v60 — Institutional RAG + IQ observability

- Minimal institutional RAG wired into prompt compilation (`store_knowledge`).
- IQ counters for scope mismatch, close miss, and dialogue-phase transitions (`observability`, `turn_metrics`).
- Scope-gate blocks emit `iq.scope_mismatch`; pipeline logs phase transitions on commerce state evolution.

## v61 — Attendance review capture

- Pipeline records attendance reviews when outbound is blocked by `commerce_clarification` or `scope_send_gate_blocked`.
- Batch classifier recognizes the same safety reasons from `ai_agent_responses` history.
- Learning insight templates added for clarification loops and scope mismatches.
- Version string: `openai-db-context-multichannel-runtime-v61`.

## Ops notes

- Tables: `ai_attendance_reviews`, `ai_learning_insights` (migration `sql/014_ai_attendance_learning.sql`).
- Hourly cron: `POST /api/cron/attendance-learning` (unchanged; reads reviews + promotes pending insights when enabled).
- Offline eval: `pytest -m offline_eval`.
