-- Apply to the dedicated agent DATABASE_URL as its server-side owner.
-- Never delete started/unknown rows to retry: reconcile the adapter session first.
BEGIN;
CREATE TABLE IF NOT EXISTS public.ai_direct_checkout_executions (
    id text PRIMARY KEY CHECK (id ~ '^[a-f0-9]{32}$'),
    workspace_id uuid NOT NULL,
    scope text NOT NULL CHECK (length(scope) = 64),
    proposal jsonb NOT NULL,
    status text NOT NULL DEFAULT 'started'
        CHECK (status IN ('started', 'completed', 'rejected', 'unknown')),
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_direct_checkout_pending
    ON public.ai_direct_checkout_executions (workspace_id, created_at)
    WHERE status IN ('started', 'unknown');
ALTER TABLE public.ai_direct_checkout_executions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ai_direct_checkout_executions FROM PUBLIC;
DO $permissions$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
        REVOKE ALL ON public.ai_direct_checkout_executions FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
        REVOKE ALL ON public.ai_direct_checkout_executions FROM authenticated;
    END IF;
END
$permissions$;
COMMIT;
