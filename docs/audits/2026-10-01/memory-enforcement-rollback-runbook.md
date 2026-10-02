# Memory enforcement rollback runbook

Prepared 2026-10-01. This document does not execute a database change. Target:
NSAgent database `fbogrbrkyorjvojeypie`, not legacy NS-db. Use an explicitly
approved operational window for a production rollback.

## Choose the rollback boundary

Before `20261001230158_memory_scope_learning_enforce.sql` has run, leave PREPARE
in place. Its added columns, audit records and indexes support both application
versions. Do not remove the new scope data to undo an application deployment.

After ENFORCE, prefer a forward fix or a rollback build that retains the scoped
repositories, incident insertion and instruction admission from this release.
Do not redeploy an unmodified release with readers/writers that omit workspace.
Disabling triggers alone is not a safe application rollback: ENFORCE also removes
the old summary/contact uniqueness keys, old UPSERTs can fail, and old reads can
mix workspaces. Recreating global uniqueness can fail when the same contact or
conversation legitimately exists in two workspaces. Never delete one workspace's
records to make a global index fit.

## Diagnose before changing enforcement

1. Record deployed agent/backend/API/worker release identities and which migrations
   have run. Stop the learning scheduler during a learning-related rollback and
   drain old worker versions before restarting work. Preserve inbound/outbox data.
2. Classify the actual database error. `memory_workspace_required` means a writer
   omitted scope; `memory_workspace_immutable` means ownership was changed;
   `learning_incident_immutable` means a caller attempted to overwrite evidence;
   `instruction_policy_validation_required` means admission proof is missing or
   stale. These are integrity checks. Fix the caller rather than weakening the
   unrelated protections.
3. Inspect the affected rows and ownership audit read-only. Keep unresolved legacy
   records quarantined. Do not derive ownership from tenant names, contact names,
   message contents or the operator's current workspace.
4. Confirm `service_role`/server access remains available. Keep RLS and all
   PUBLIC/anon/authenticated grant revocations intact during rollback.

## Temporary trigger relaxation, only with scoped application code retained

If a confirmed trigger defect cannot be fixed immediately, an operator may choose
to temporarily disable only that trigger on the affected table. This is a
separate approved action; it is not authorization to run an unscoped old release.
Keep application validation enabled, record the start/end time and table, and
pause the corresponding writer if its application-level guarantee is uncertain.

The exact trigger-to-table mapping is:

| Trigger | Tables |
| --- | --- |
| `require_memory_workspace` | `ai_contact_memories`, `ai_conversation_summaries`, `ai_memory_proposals`, `ai_prompt_compilations` |
| `preserve_learning_incident` | `ai_learning_cases` |
| `guard_instruction_activation` | `ai_agent_instruction_extensions` |

For example, if the defect is limited to the prompt trigger and all deployed prompt
writers still supply a verified workspace, the approved operator can run:

```sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE public.ai_prompt_compilations DISABLE TRIGGER require_memory_workspace;
COMMIT;
```

Never use `DISABLE TRIGGER ALL` or change `session_replication_role`. Do not disable
RLS, drop workspace indexes, remove audit data, regrant browser-role access or
alter the frozen migration history.

After deploying the fix, inspect all rows created/changed during the exception
window and restore the exact trigger before resuming the paused writer:

```sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE public.ai_prompt_compilations ENABLE TRIGGER require_memory_workspace;
COMMIT;
```

Re-enabling a trigger does not validate historical rows. Any missing/ambiguous
scope or mutated incident discovered during the exception needs a reviewed repair;
do not automatically attribute it to a workspace.

## Verify recovery

- All serving processes run the chosen scoped release; a successful deployment
  alone does not prove old background workers drained.
- One scoped read/write per memory repository succeeds; a missing-workspace write
  and a cross-workspace read remain blocked. Use a controlled workspace/test record.
- Prompt-to-response trace joins match both workspace and inbound ID.
- Re-approving an already activated instruction reconciles pending learning cases
  without duplicating or replacing incidents. Its `case_materialization` receipt
  lists all original `source_review_ids` and resulting case IDs.
- No automatic promotion is enabled as an incidental rollback change. Compare
  actual runtime configuration to the recorded pre-rollback version.
- Verify the expected triggers are enabled and server access/browser denials are
  unchanged. Preserve the resulting checks in the deployment record.
