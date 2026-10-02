# F08/F09/F13/F16 — implementation and verification

Inspected on 2026-10-01. This specialist review used offline tests and read-only inspection. The subsequent authorized production rollout, performed by the coordinating agent, is recorded in `release-verification.json`.

## Implementation

- F08: versioned canonical commercial policy is always included in the fixed prompt. Extension admission validates script, hidden characters, known policy conflicts, normalized/near duplicates and opposing instructions. Both backend approval and agent auto-promotion run the same stdlib policy contract. The enforced database trigger requires the validation report before activation; runtime formatting also filters conflicting legacy extensions. These deterministic checks do not certify every possible natural-language contradiction; the existing human review remains necessary.
- F09: incident keys include source turn identity; a retry uses `ON CONFLICT DO NOTHING`, retaining the original example/correction. Every reviewed sample in an activated learning cluster is retained, not only the first sample. A separate aggregate view represents failure patterns. Closing acknowledgments without a renewed question are not labeled failed/unclear clarification. Reviewed cases can be exported through `python -m app.evaluation.learning_case_export --incident ... --review ... --output ...`; the independent review must provide conversation context and acceptance criteria. The output preserves reviewer provenance and source hash. Automatic annotation or unreviewed activation is not performed.
- F13: contact memory, summary, proposal and prompt writes carry workspace ownership. Reads without a resolved workspace return no memory, and writes fail closed. The cache key includes the resolved workspace. The prompt audit stores the effective config hash, selected memory IDs, inbound and trace; persistence links the resulting response using matching workspace and inbound. Legacy ownership is reconstructed only from unambiguous recorded inbound/response ownership; unresolved/conflicting records remain quarantined and are retained for review.

## Migration order

1. Apply backend `20261001230157_memory_scope_learning_prepare.sql`. It is compatible with old writers, adds scope/audit information and performs the first evidence-based backfill.
2. Deploy both the agent and backend changes. Drain all old API/worker processes.
3. Apply `20261001230158_memory_scope_learning_enforce.sql`. It reconciles records written between phases, switches unique keys to workspace scope, and enables strict ownership/immutable incident/extension activation triggers.
4. After ENFORCE, retain the scoped repositories even when rolling back unrelated behavior. Disabling triggers alone does not make an unscoped old release compatible or safe: old uniqueness keys were removed and unscoped readers can join workspaces. See `memory-enforcement-rollback-runbook.md` for targeted recovery. Do not revive quarantined records by guessing a tenant/workspace.

The remote NSAgent schema was checked read-only: expected source columns and UUID workspace columns exist, `ai_learning_cases.failure_codes` is JSONB, and existing unique indexes/constraints match the migration assumptions.

## F16 legacy NS-db dependency review

Target is **NS-db `mgwvavsjxzwflibcjzhd`**, not the NSAgent database. Read-only PostgreSQL catalog checks found:

- 55 public tables, all owned by `postgres`; 35 without RLS; one public view; 56 table/view objects with broad `anon` and `authenticated` grants; no public row policies.
- `postgres` and `service_role` both have `BYPASSRLS`.
- Three active Supavisor sessions used `postgres`; their last query families included `payments` and `draws`. No customer SQL literals were exported.
- Local backend `newstore-backend-pix-full-ajustes/lancaster_backend/src/db/pg.js` uses direct PostgreSQL pools, prioritizing `DATABASE_URL`.
- Frontend `newstore-sorteios/newstore-sorteios/src/services/me.js` retrieves business data through HTTP API. The only `.from()` Supabase database call found in this frontend is a diagnostic ping to `teste`, a table which the database catalog confirms does not exist.
- The available production env export is redacted; the database-role conclusion comes from the PostgreSQL catalog/session evidence, not from fabricated inspection of a secret.

`Chatbo-backendAgent/supabase/legacy-nsdb/supabase/migrations/20261001230529_restrict_legacy_server_only.sql` is the isolated, versioned proposal. It enables RLS and revokes public/browser-role table, view, sequence and application function access. Existing server access is preserved; it does not invent customer ownership policies. Preconditions abort if object ownership or server BYPASSRLS properties changed. The application API continues to enforce customer authorization. Production application remains pending separate authorization.

Function defaults require a global `ALTER DEFAULT PRIVILEGES ... REVOKE EXECUTE ... FROM PUBLIC`; a schema-only revoke cannot remove PostgreSQL's global default. This was caught by the isolated test and corrected. Existing functions in other schemas are not changed.

## Verification

- Combined agent memory/persona/learning/identity/prompt/context-review/export/budget suite: 350 passed. A further focused quality/critique/call-policy run passed 60 tests.
- Manual backend approval now validates every source review in the same workspace before activation and materializes incidents using the same turn identity as NSAgent. A stored receipt preserves the complete original `source_review_ids` and the review-to-case mapping. Retries read existing incidents without replacing evidence. Partial persistence returns an explicit activated-but-pending error so approval can be retried. No DDL is required for this hook.
- Backend learning approval suite: 12 passed, including 141 source reviews across multiple batches. Agent learning suite after the manual hook/turn-ID completion: 70 passed.
- `scripts/verify_memory_security_migrations.mjs` reads the versioned migration files from the sibling backend checkout and runs real PostgreSQL semantics in **PGlite 0.5.8 / PostgreSQL 18.3**, with synthetic fixtures and no production connection. All 17 recorded criteria passed, including deny/allow CRUD, views, sequences and RPCs, future privileges, backfill ambiguity, rollout compatibility, immutability, workspace uniqueness, bootstrap behavior after enforcement and migration idempotence.
- Machine-readable result: `security-isolation-verification.json` in this directory.
- PGlite proves SQL behavior on the fixtures. The later production checks in `release-verification.json` separately confirm both authorized NsAgent memory migrations, deployed versions, six enabled triggers and retained server access. The legacy NS-db permission migration was not applied.
