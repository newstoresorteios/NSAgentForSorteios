-- Additive repair: no review deletion and no customer data updates.
-- Metadata is read from existing provider_response JSON; no response_metadata column is required.
BEGIN;
CREATE TABLE IF NOT EXISTS public.ai_learning_cursors (
    tenant_id text PRIMARY KEY,
    last_response_id bigint,
    last_response_at timestamptz,
    last_run_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS public.ai_learning_cases (
    id bigserial PRIMARY KEY,
    tenant_id text NOT NULL,
    case_key text NOT NULL,
    conversation_key text,
    failure_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    customer_excerpt text NOT NULL DEFAULT '',
    bad_reply text NOT NULL DEFAULT '',
    correction text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'retired', 'rejected')),
    insight_id bigint,
    importance numeric(5,4) NOT NULL DEFAULT 0.5,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_learning_cases_key
ON public.ai_learning_cases (tenant_id, case_key);

CREATE INDEX IF NOT EXISTS idx_ai_learning_cases_active
ON public.ai_learning_cases (tenant_id, status, importance DESC, updated_at DESC);

ALTER TABLE public.ai_learning_cursors ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_learning_cases ENABLE ROW LEVEL SECURITY;
COMMIT;
