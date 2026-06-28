CREATE TABLE IF NOT EXISTS public.ai_user_preferences (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  preferred_name text NULL,
  ask_preferred_name boolean NOT NULL DEFAULT false,
  last_preferred_name_prompt_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ai_user_preferences_user_id_unique UNIQUE (user_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_user_preferences_user_id
ON public.ai_user_preferences(user_id);
