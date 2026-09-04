-- Continuous attendance learning: incremental cursor, idempotent reviews, case bank.

DELETE FROM public.ai_attendance_reviews AS newer
USING public.ai_attendance_reviews AS older
WHERE newer.response_id IS NOT NULL
  AND newer.tenant_id = older.tenant_id
  AND newer.response_id = older.response_id
  AND newer.id > older.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_attendance_reviews_response
ON public.ai_attendance_reviews (tenant_id, response_id)
WHERE response_id IS NOT NULL;

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
