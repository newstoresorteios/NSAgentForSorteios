-- One shared analysis per workspace/account/Story and processing version.
CREATE TABLE public.instagram_story_analysis_jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
    tenant_id text NOT NULL,
    provider text NOT NULL,
    instagram_account_id text NOT NULL,
    story_media_id text NOT NULL,
    analysis_version text NOT NULL,
    generation uuid NOT NULL DEFAULT gen_random_uuid(),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','ready','failed')),
    source_inbound_id bigint NOT NULL,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    media_storage_path text,
    media_sha256 text,
    media_mime text,
    media_bytes bigint,
    attempts integer NOT NULL DEFAULT 0,
    lease_owner uuid,
    lease_until timestamptz,
    available_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '7 days',
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, tenant_id, provider, instagram_account_id, story_media_id, analysis_version)
);
CREATE INDEX instagram_story_jobs_claim_idx ON public.instagram_story_analysis_jobs(available_at, id)
    WHERE status IN ('pending','processing');
ALTER TABLE public.instagram_story_analysis_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.instagram_story_analysis_jobs FROM anon, authenticated;
GRANT ALL ON public.instagram_story_analysis_jobs TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.instagram_story_analysis_jobs_id_seq TO service_role;
ALTER TABLE public.ai_inbound_inbox ADD COLUMN story_analysis_job_id bigint
    REFERENCES public.instagram_story_analysis_jobs(id) ON DELETE SET NULL;
CREATE INDEX ai_inbox_story_job_idx ON public.ai_inbound_inbox(story_analysis_job_id)
    WHERE story_analysis_job_id IS NOT NULL;

-- Synchronize Story controls with the deployed multimodal implementation.
-- Catalog defaults alone cannot replace values already published by a workspace.
INSERT INTO public.agent_configuration_catalog (key, definition)
SELECT item->>'key', item
FROM jsonb_array_elements($story_catalog$[
  {
    "key": "instagram_story_media_max_bytes",
    "attribute": "instagram_story_media_max_bytes",
    "target": "setting",
    "label": "Instagram story media max bytes",
    "description": "Parâmetro INSTAGRAM_STORY_MEDIA_MAX_BYTES. Aplicado na próxima conversa após publicação.",
    "group": "Atendimento",
    "type": "integer",
    "default": 104857600,
    "readOnly": false,
    "min": 1024,
    "max": 536870912
  },
  {
    "key": "instagram_story_worker_enabled",
    "attribute": "instagram_story_worker_enabled",
    "target": "setting",
    "label": "Worker de análise de Stories",
    "description": "Processa mídia e valida a identidade em uma fila compartilhada por Story.",
    "group": "Atendimento",
    "type": "boolean",
    "readOnly": false,
    "default": true
  }
]$story_catalog$::jsonb) AS item
ON CONFLICT (key) DO UPDATE
SET definition = EXCLUDED.definition, updated_at = now()
WHERE public.agent_configuration_catalog.definition IS DISTINCT FROM EXCLUDED.definition;

-- This activation belongs only to the New Store commercial agent. Resolve its
-- workspace from the existing active persona instead of embedding generated IDs.
-- Keep every unrelated setting and record a new version through the normal API.
DO $story_activation$
DECLARE
    agent public.workspace_agents%ROWTYPE;
    values_before jsonb;
    patch_values constant jsonb := $story_values${
  "instagram_story_media_max_bytes": 104857600,
  "instagram_story_worker_enabled": true
}$story_values$::jsonb;
BEGIN
    FOR agent IN
        SELECT a.* FROM public.workspace_agents a
        WHERE a.agent_type = 'nsagent' AND a.status = 'active'
          AND EXISTS (
              SELECT 1 FROM public.ai_agent_persona_versions p
              WHERE p.workspace_id = a.workspace_id AND p.status = 'active'
                AND p.tenant_id = 'newstore' AND p.persona_key = 'newstore_commercial'
          )
        FOR UPDATE OF a
    LOOP
        values_before := coalesce(agent.configuration #> '{runtime,values}', '{}'::jsonb);
        IF NOT values_before @> patch_values THEN
            PERFORM public.publish_workspace_agent_config(
                agent.workspace_id,
                agent.configuration || jsonb_build_object('runtime',
                    coalesce(agent.configuration->'runtime', '{}'::jsonb)
                    || jsonb_build_object('schemaVersion', 2, 'values', values_before || patch_values)),
                'migration:story-analysis-worker',
                agent.config_version
            );
        END IF;
    END LOOP;
END
$story_activation$;
