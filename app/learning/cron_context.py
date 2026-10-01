"""Bind scheduled jobs to the active workspace of this agent deployment."""
from contextlib import asynccontextmanager
import asyncio

from app.config import get_settings
from app.db import get_conn
from app.configuration.runtime import bind_bundle, reset_bundle, settings_from_bundle
from app.persona.persona_runtime import load_persona_runtime, set_persona_runtime, reset_persona_runtime


def load_scheduled_persona():
    settings = get_settings()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT p.workspace_id
                FROM public.ai_agent_persona_versions p
                JOIN public.workspace_agents wa ON wa.workspace_id=p.workspace_id
                WHERE p.tenant_id=%s AND p.persona_key=%s AND p.status='active'
                  AND wa.agent_type='nsagent' AND wa.status='active'""",
                (settings.agent_persona_tenant_id, settings.agent_persona_key))
            rows = cur.fetchall()
    # A tenant-wide cursor and deployment credentials cannot safely serve two workspaces.
    if len(rows) != 1:
        raise RuntimeError("scheduled_workspace_missing_or_ambiguous")
    return load_persona_runtime(workspace_id=str(rows[0]["workspace_id"]))


@asynccontextmanager
async def scheduled_workspace():
    persona = await asyncio.to_thread(load_scheduled_persona)
    if not persona.workspace_id or not persona.configuration_bundle:
        raise RuntimeError("scheduled_workspace_configuration_missing")
    bundle = {**persona.configuration_bundle, "workspace_id": persona.workspace_id}
    binding = bind_bundle(bundle, settings_from_bundle(get_settings(), bundle))
    token = set_persona_runtime(persona)
    try:
        yield persona
    finally:
        reset_persona_runtime(token)
        reset_bundle(binding)
