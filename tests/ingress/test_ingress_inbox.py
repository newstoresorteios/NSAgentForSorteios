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
