"""Attach audited prompts to the persisted response in the same workspace/turn."""
from app.db import get_conn


def link_prompt_response(*, workspace_id: str, inbound_id: int, response_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.ai_prompt_compilations p SET response_id = r.id
                   FROM public.ai_agent_responses r
                   WHERE p.workspace_id = %s::uuid AND p.inbound_id = %s
                     AND r.id = %s AND r.workspace_id = p.workspace_id
                     AND r.inbound_id = p.inbound_id AND p.response_id IS NULL""",
                (workspace_id, inbound_id, response_id),
            )
            return int(cur.rowcount or 0)
