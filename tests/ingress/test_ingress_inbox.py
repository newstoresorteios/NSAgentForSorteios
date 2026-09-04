from pathlib import Path

from app.ingress.inbox import build_idempotency_key
from app.ingress.reconstruct import incoming_from_inbox_payload


def test_idempotency_prefers_message_id():
    key = build_idempotency_key(
        provider="brevo",
        message_id="msg-1",
        payload={"a": 1},
    )
    assert key == "brevo:msg:msg-1"


def test_idempotency_falls_back_to_payload_hash():
    key_a = build_idempotency_key(
        provider="meta",
        message_id=None,
        payload={"text": "oi", "n": 1},
    )
    key_b = build_idempotency_key(
        provider="meta",
        message_id="",
        payload={"text": "oi", "n": 1},
    )
    key_c = build_idempotency_key(
        provider="meta",
        message_id=None,
        payload={"text": "oi", "n": 2},
    )
    assert key_a.startswith("meta:hash:")
    assert key_a == key_b
    assert key_a != key_c


def test_claim_sql_reclaims_expired_leased_rows():
    inbox_sql = Path("app/ingress/inbox.py").read_text(encoding="utf-8")
    outbox_sql = Path("app/ingress/outbox.py").read_text(encoding="utf-8")
    reclaim = "status = 'leased'"
    expired = "lease_expires_at < now()"
    assert reclaim in inbox_sql and expired in inbox_sql
    assert reclaim in outbox_sql and expired in outbox_sql


def test_reconstruct_fills_conversation_id_from_payload_top_level():
    incoming = incoming_from_inbox_payload(
        {
            "normalized": {
                "provider": "brevo",
                "channel": "whatsapp",
                "text": "oi",
                "message_id": "msg-cid",
                "sender_key": "whatsapp:5511999999999",
            },
            "conversation_id": "wa-thread-9",
            "raw": {},
        }
    )
    assert incoming is not None
    assert incoming.conversation_id == "wa-thread-9"


def test_reconstruct_reads_normalized_brevo_shape():
    incoming = incoming_from_inbox_payload(
        {
            "normalized": {
                "provider": "brevo",
                "channel": "whatsapp",
                "text": "oi",
                "message_id": "msg-9",
                "sender_key": "whatsapp:5511999999999",
            },
            "raw": {"eventName": "conversationFragment"},
        }
    )
    assert incoming is not None
    assert incoming.channel == "whatsapp"
    assert incoming.text == "oi"
    assert incoming.message_id == "msg-9"


def test_inbox_conversation_key_fills_missing_conversation_id(monkeypatch):
    import asyncio

    from app.ingress import worker as worker_mod
    from app.models import AgentResult, IncomingMessage
    from app.ops.runtime_context import get_current_turn

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        worker_mod,
        "incoming_from_inbox_payload",
        lambda *_args, **_kwargs: IncomingMessage(
            provider="brevo",
            channel="whatsapp",
            text="oi",
            sender_key="whatsapp:5511999999999",
        ),
    )
    monkeypatch.setattr(worker_mod, "is_caption_echo_of_recent_image", lambda _incoming: False)
    monkeypatch.setattr(worker_mod, "attach_recent_image_for_followup", lambda incoming: incoming)
    monkeypatch.setattr(worker_mod, "claim_inbound_message", lambda *_a, **_k: (True, 11))
    monkeypatch.setattr("app.ops.human_takeover.human_takeover_active", lambda *_a, **_k: False)
    monkeypatch.setattr(worker_mod, "has_successful_agent_response", lambda *_a, **_k: False)

    async def fake_process(incoming, _context):
        runtime = get_current_turn()
        captured["conversation_id"] = incoming.conversation_id
        captured["enforce"] = runtime.llm_budget.enforce if runtime else None
        return AgentResult(reply_text="ok", intent="commerce")

    async def fake_send(_incoming, _result):
        return {"ok": True}

    monkeypatch.setattr(
        "app.message_pipeline.process_incoming_message",
        fake_process,
    )
    monkeypatch.setattr(worker_mod, "_send_reply", fake_send)
    monkeypatch.setattr(worker_mod, "insert_agent_response", lambda *_a, **_k: None)
    monkeypatch.setattr(worker_mod, "mark_inbox_processed", lambda *_a, **_k: None)
    monkeypatch.setattr(worker_mod, "mark_inbox_failed", lambda *_a, **_k: None)

    result = asyncio.run(
        worker_mod.process_inbox_row(
            {
                "id": 5,
                "payload_json": {},
                "attempts": 1,
                "conversation_key": "wa-thread-history",
            }
        )
    )
    assert result["ok"] is True
    assert captured["conversation_id"] == "wa-thread-history"
    assert captured["enforce"] is True


def test_ingress_exception_handlers_are_not_bare_pass():
    import ast

    silent: list[str] = []
    for path in Path("app/ingress").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = ast.unparse(node.type) if node.type is not None else "BaseException"
            if caught not in {"Exception", "BaseException"}:
                continue
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                silent.append(f"{path.as_posix()}:{node.lineno}")
    assert silent == []
