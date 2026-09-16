import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
path = Path(sys.argv[1]).resolve()
if path.read_text(encoding="utf-8").strip():
    raise SystemExit("Refusing to overwrite a nonempty migration")
fields = json.loads((root / "sql/seeds/operator_catalog.json").read_text(encoding="utf-8"))
for field in fields:
    if field["key"] == "agent_max_persona_attachments":
        field.update(readOnly=True, description="Compatibilidade com versões anteriores. Use Agent knowledge attachment limit para definir quantos anexos participam da busca.")
sql = f"""INSERT INTO public.agent_configuration_catalog(key,definition)
SELECT item->>'key',item FROM jsonb_array_elements($catalog_seed${json.dumps(fields, ensure_ascii=False)}$catalog_seed$::jsonb) item
ON CONFLICT(key) DO UPDATE SET definition=excluded.definition,updated_at=now();

CREATE SCHEMA IF NOT EXISTS extensions;
ALTER EXTENSION pg_trgm SET SCHEMA extensions;
GRANT USAGE ON SCHEMA extensions TO service_role;
-- Keep the deployed legacy query callable throughout the rollout.
CREATE OR REPLACE FUNCTION public.similarity(text,text) RETURNS real
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SECURITY INVOKER SET search_path=public,pg_temp
AS 'SELECT extensions.similarity($1,$2)';
REVOKE ALL ON FUNCTION public.similarity(text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.similarity(text,text) TO service_role;
CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_title_coalesce_trgm
 ON public.ai_catalog_index USING gin ((lower(coalesce(title_normalized,''))) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_ai_catalog_available_constraints
 ON public.ai_catalog_index(tenant_id,lower(coalesce(brand,'')),freshness_at DESC)
 WHERE available IS DISTINCT FROM false AND (stock IS NULL OR stock>0);
CREATE INDEX IF NOT EXISTS idx_agent_responses_workspace_cursor
 ON public.ai_agent_responses(workspace_id,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_conversas_workspace_latest
 ON public.conversas(workspace_id,last_message_at DESC,id DESC);
ALTER TABLE public.agent_persona_attachments ADD COLUMN IF NOT EXISTS valid_until timestamptz;
ALTER TABLE public.agent_persona_attachments ADD COLUMN IF NOT EXISTS content_hash text
 GENERATED ALWAYS AS (md5(coalesce(extracted_text,''))) STORED;
CREATE INDEX IF NOT EXISTS idx_persona_documents_latest
 ON public.agent_persona_attachments(persona_id,status,updated_at DESC);

-- The list only carries the compact runtime fields it displays; details remain
-- behind the workspace-scoped endpoint and table RLS.
CREATE OR REPLACE VIEW public.ai_agent_trace_summaries WITH(security_invoker=true) AS
SELECT id,inbound_id,workspace_id,channel,intent,handoff_required,safety_reason,provider_send_ok,created_at,
 jsonb_build_object('_agent_metadata',jsonb_build_object(
  'response_source',provider_response#>'{{_agent_metadata,response_source}}',
  'persona_runtime',jsonb_build_object(
   'persona_version_id',provider_response#>'{{_agent_metadata,persona_runtime,persona_version_id}}',
   'configuration_version',provider_response#>'{{_agent_metadata,persona_runtime,configuration_version}}',
   'runtime_configuration_count',provider_response#>'{{_agent_metadata,persona_runtime,runtime_configuration_count}}'),
  'turn_runtime',(SELECT coalesce(jsonb_object_agg(key,value),'{{}}'::jsonb)
    FROM jsonb_each(CASE WHEN jsonb_typeof(provider_response#>'{{_agent_metadata,turn_runtime}}')='object' THEN provider_response#>'{{_agent_metadata,turn_runtime}}' ELSE '{{}}'::jsonb END)
    WHERE key=ANY(ARRAY['trace_id','channel','execution_path','processing_total_ms','openai_call_count','tray_call_count',
      'database_call_count','openai_input_tokens','openai_output_tokens','fallback_reasons','inbound','outbound']))
 )) AS provider_response
FROM public.ai_agent_responses;
REVOKE ALL ON public.ai_agent_trace_summaries FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.ai_agent_trace_summaries TO service_role;
NOTIFY pgrst,'reload schema';
"""
path.write_text(sql,encoding="utf-8")
(root / "sql/seeds/operator_catalog.json").write_text(json.dumps(fields,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
print(json.dumps({"migration":path.name,"catalog_fields":len(fields)}))
