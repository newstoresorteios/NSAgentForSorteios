"""Durable at-most-once claims; commit before making any external mutation."""
from __future__ import annotations

from app.db import get_conn, to_jsonb


class CheckoutJournal:
    def claim(self, proposal: dict) -> tuple[bool, dict]:
        from app.evaluation.context import prohibit_side_effect
        prohibit_side_effect("direct_checkout_claim")
        args = {"id": proposal["id"], "workspace": proposal["workspace"],
                "scope": proposal["scope"], "proposal": to_jsonb(proposal)}
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '5s'")
                cur.execute("SET LOCAL lock_timeout = '2s'")
                cur.execute("""
                    INSERT INTO public.ai_direct_checkout_executions
                        (id, workspace_id, scope, proposal)
                    VALUES (%(id)s, %(workspace)s::uuid, %(scope)s, %(proposal)s)
                    ON CONFLICT (id) DO NOTHING RETURNING id
                """, args)
                owned = cur.fetchone() is not None
                cur.execute("""
                    SELECT status, result, proposal FROM public.ai_direct_checkout_executions
                    WHERE id=%(id)s AND workspace_id=%(workspace)s::uuid AND scope=%(scope)s
                """, args)
                row = cur.fetchone()
                if not row or row["proposal"] != proposal:
                    raise ValueError("checkout_claim_mismatch")
        return owned, row

    def finish(self, proposal: dict, status: str, result: dict) -> None:
        if status not in {"completed", "rejected", "unknown"}:
            raise ValueError("invalid_checkout_status")
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '5s'")
                cur.execute("SET LOCAL lock_timeout = '2s'")
                cur.execute("""
                    UPDATE public.ai_direct_checkout_executions
                    SET status=%(status)s, result=%(result)s, updated_at=now()
                    WHERE id=%(id)s AND workspace_id=%(workspace)s::uuid
                      AND scope=%(scope)s AND status='started'
                    RETURNING id
                """, {"id": proposal["id"], "workspace": proposal["workspace"],
                        "scope": proposal["scope"], "status": status, "result": to_jsonb(result)})
                if cur.fetchone() is None:
                    raise RuntimeError("checkout_claim_not_owned")
