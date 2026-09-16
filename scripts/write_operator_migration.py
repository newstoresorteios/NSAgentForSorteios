"""Populate an empty CLI-created migration with reviewed catalog and atomic RPCs."""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
path = Path(sys.argv[1]).resolve()
if path.read_text(encoding="utf-8").strip():
    raise SystemExit("Refusing to overwrite a nonempty migration")
fields = json.loads((root / "sql/seeds/operator_catalog.json").read_text(encoding="utf-8"))
names = {
    "name":"name", "role":"role", "segment":"segment", "language":"language", "tone":"tone",
    "tone_details":"toneDetails", "greeting":"greeting", "introduction":"introduction",
    "customer_address_style":"customerAddressStyle", "closing_message":"closingMessage",
    "target_audience":"targetAudience", "customer_profile":"customerProfile", "sales_goals":"salesGoals",
    "qualification_rules":"qualificationRules", "opportunity_criteria":"opportunityCriteria",
    "human_handoff_criteria":"humanHandoffCriteria", "objection_handling":"objectionHandling",
    "upsell_rules":"upsellRules", "recommendation_rules":"recommendationRules", "escalation_rules":"escalationRules",
    "restrictions":"restrictions", "examples":"examples",
}
pairs = ",\n".join(f"'{camel}',j->'{snake}'" for snake,camel in names.items())
sql = f"""-- No application content is read from source files at runtime.
INSERT INTO public.agent_configuration_catalog(key,definition)
SELECT item->>'key',item FROM jsonb_array_elements($catalog_seed${json.dumps(fields, ensure_ascii=False)}$catalog_seed$::jsonb) item
ON CONFLICT(key) DO UPDATE SET definition=excluded.definition,updated_at=now();

CREATE OR REPLACE FUNCTION public.persona_profile_snapshot(j jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=public,pg_temp AS $snapshot$
SELECT j || jsonb_build_object({pairs},'workspaceId',j->'workspace_id');
$snapshot$;
REVOKE ALL ON FUNCTION public.persona_profile_snapshot(jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.persona_profile_snapshot(jsonb) TO service_role;
"""
original = (path.parent / "20260915052534_atomic_workspace_persona_publication.sql").read_text(encoding="utf-8")
publisher = original[original.index("CREATE OR REPLACE FUNCTION public.publish_nsagent_persona"):original.index("-- Recover the last")]
publisher = publisher.replace("to_jsonb(old_p)", "public.persona_profile_snapshot(to_jsonb(old_p))").replace("to_jsonb(p)", "public.persona_profile_snapshot(to_jsonb(p))")
sql += publisher
columns = ",".join(names)
selected = ",".join("candidate."+name for name in names)
allowed = ",".join("'"+name+"'" for name in names)
sql += f"""
CREATE OR REPLACE FUNCTION public.update_and_publish_nsagent_persona(
 p_workspace_id uuid,p_persona_id uuid,p_expected_profile_version integer,p_patch jsonb,
 p_tenant_id text,p_persona_key text,p_instructions text,p_instructions_hash text,
 p_activated_by text DEFAULT NULL,p_knowledge_docs integer DEFAULT 0
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $edit$
DECLARE original public.agent_personas%ROWTYPE; candidate public.agent_personas%ROWTYPE;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(p_tenant_id || ':' || p_persona_key,0));
 SELECT * INTO original FROM public.agent_personas WHERE id=p_persona_id AND workspace_id=p_workspace_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'invalid_persona_workspace_link' USING ERRCODE='23514'; END IF;
 IF original.version<>p_expected_profile_version THEN RAISE EXCEPTION 'persona_version_conflict' USING ERRCODE='40001'; END IF;
 IF original.status<>'active' THEN RAISE EXCEPTION 'persona_not_active' USING ERRCODE='23514'; END IF;
 IF jsonb_typeof(p_patch)<>'object' OR EXISTS(SELECT FROM jsonb_object_keys(p_patch) k WHERE k <> ALL(ARRAY[{allowed}]))
 THEN RAISE EXCEPTION 'invalid_persona_patch' USING ERRCODE='23514'; END IF;
 SELECT * INTO candidate FROM jsonb_populate_record(original,p_patch);
 UPDATE public.agent_personas SET ({columns})=({selected}),version=version+1,updated_at=now()
 WHERE id=p_persona_id AND workspace_id=p_workspace_id RETURNING * INTO candidate;
 INSERT INTO public.agent_persona_versions(persona_id,workspace_id,version,snapshot,change_type)
 VALUES(candidate.id,candidate.workspace_id,candidate.version,public.persona_profile_snapshot(to_jsonb(candidate)),'updated');
 RETURN public.publish_nsagent_persona(p_workspace_id,p_persona_id,candidate.version,p_tenant_id,p_persona_key,
  p_instructions,p_instructions_hash,p_activated_by,p_knowledge_docs);
END $edit$;
REVOKE ALL ON FUNCTION public.update_and_publish_nsagent_persona(uuid,uuid,integer,jsonb,text,text,text,text,text,integer) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.update_and_publish_nsagent_persona(uuid,uuid,integer,jsonb,text,text,text,text,text,integer) TO service_role;
NOTIFY pgrst,'reload schema';
"""
path.write_text(sql, encoding="utf-8")
print(json.dumps({"migration":path.name,"fields":len(fields),"bytes":len(sql.encode())}))
