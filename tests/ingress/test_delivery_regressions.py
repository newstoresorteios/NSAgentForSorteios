"""Offline delivery races, accepted-turn recovery and ordering contracts."""
import asyncio
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, MagicMock

import pytest
from app.ingress import outbox, outbox_worker, worker, inbox
from app.models import IncomingMessage, AgentResult


def _accepted(status="failed"):
    incoming = IncomingMessage(provider="brevo", channel="whatsapp", text="fake", conversation_id="fake-conv", sender_key="fake-sender")
    result = AgentResult(reply_text="Accepted immutable answer", intent="commerce", response_metadata={"version":"original"})
    return {"id":7,"inbound_id":9,"provider":"brevo","channel":"whatsapp","status":status,
        "reply_text":result.reply_text,"reply_payload":outbox.build_outbound_envelope(incoming,result),
        "lease_owner":"fake-owner","attempts":1,"max_attempts":5}


@pytest.mark.asyncio
async def test_inline_and_retry_cannot_both_send_same_claim(monkeypatch):
    row = _accepted("pending")
    claimed = False
    started, release = asyncio.Event(), asyncio.Event()
    def claim(_id):
        nonlocal claimed
        if claimed:
            return None
        claimed = True
        return row
    async def send(incoming,result):
        assert result.reply_text == row["reply_text"]
        started.set()
        await release.wait()
        return {"ok":True}
    monkeypatch.setattr(outbox,"claim_outbox_for_send",claim)
    monkeypatch.setattr(outbox,"get_outbox_status",lambda _:"leased")
    marked = Mock()
    monkeypatch.setattr(outbox,"mark_outbox_sent",marked)
    first = asyncio.create_task(outbox.dispatch_accepted_outbound(7,send))
    await started.wait()
    second_send = AsyncMock()
    second = await outbox.dispatch_accepted_outbound(7,second_send)
    assert second["queued"] is True
    second_send.assert_not_awaited()
    release.set()
    assert (await first)["ok"] is True
    assert marked.call_args.kwargs["owner"] == "fake-owner"


@pytest.mark.asyncio
async def test_sent_receipt_prevents_resend(monkeypatch):
    monkeypatch.setattr(outbox,"claim_outbox_for_send",lambda _:None)
    monkeypatch.setattr(outbox,"get_outbox_status",lambda _:"sent")
    send = AsyncMock()
    assert (await outbox.dispatch_accepted_outbound(7,send))["ok"] is True
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_receipt_persistence_failure_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(outbox,"claim_outbox_for_send",lambda _:_accepted())
    monkeypatch.setattr(outbox,"mark_outbox_sent",Mock(side_effect=RuntimeError("fake database failure")))
    with pytest.raises(RuntimeError):
        await outbox.dispatch_accepted_outbound(7,AsyncMock(return_value={"ok":True}))


@pytest.mark.asyncio
@pytest.mark.parametrize("status",["pending","failed","leased","dead"])
async def test_worker_retries_accepted_envelope_without_regenerating(monkeypatch,status):
    import app.message_pipeline as pipeline
    import app.ops.human_takeover as takeover
    row = _accepted(status)
    incoming = outbox.incoming_from_outbox_row(row)
    monkeypatch.setattr(worker,"incoming_from_inbox_payload",lambda _:incoming)
    monkeypatch.setattr(worker,"is_caption_echo_of_recent_image",lambda _:False)
    monkeypatch.setattr(worker,"attach_recent_image_for_followup",lambda v:v)
    monkeypatch.setattr(worker,"claim_inbound_message",lambda _: (False,9))
    monkeypatch.setattr(worker,"has_successful_agent_response",lambda _:False)
    monkeypatch.setattr(outbox,"has_sent_outbound",lambda _:False)
    monkeypatch.setattr(outbox,"get_accepted_outbound",lambda _:row)
    monkeypatch.setattr(takeover,"human_takeover_active",lambda _:False)
    generate = AsyncMock(side_effect=AssertionError("must not regenerate accepted turn"))
    monkeypatch.setattr(pipeline,"process_incoming_message",generate)
    monkeypatch.setattr(worker,"enqueue_accepted_outbound",lambda **_:7)
    monkeypatch.setattr(outbox,"dispatch_accepted_outbound",AsyncMock(return_value={"ok":True}))
    saved = Mock(return_value=1)
    monkeypatch.setattr(worker,"insert_agent_response",saved)
    monkeypatch.setattr(worker,"mark_inbox_processed",Mock())
    monkeypatch.setattr("app.learning.attendance_learning.attach_response_id_to_pipeline_reviews",Mock())
    assert (await worker.process_inbox_row({"id":1,"payload_json":{}},lock_held=True))["ok"] is True
    generate.assert_not_awaited()
    assert saved.call_args.args[0]["reply_text"] == row["reply_text"]


@pytest.mark.asyncio
async def test_worker_sent_outbox_survives_missing_response_audit(monkeypatch):
    import app.ops.human_takeover as takeover
    incoming = outbox.incoming_from_outbox_row(_accepted())
    monkeypatch.setattr(worker,"incoming_from_inbox_payload",lambda _:incoming)
    monkeypatch.setattr(worker,"is_caption_echo_of_recent_image",lambda _:False)
    monkeypatch.setattr(worker,"attach_recent_image_for_followup",lambda v:v)
    monkeypatch.setattr(worker,"claim_inbound_message",lambda _: (False,9))
    monkeypatch.setattr(worker,"has_successful_agent_response",lambda _:False)
    monkeypatch.setattr(outbox,"has_sent_outbound",lambda _:True)
    monkeypatch.setattr(takeover,"human_takeover_active",lambda _:False)
    monkeypatch.setattr(worker,"mark_inbox_processed",Mock())
    result = await worker.process_inbox_row({"id":1,"payload_json":{}},lock_held=True)
    assert result["skipped"] == "already_sent"


@pytest.mark.asyncio
async def test_explicit_brevo_instagram_is_not_rerouted_by_meta_setting(monkeypatch):
    import app.channels.brevo_client as brevo
    import app.channels.meta_instagram as meta
    monkeypatch.setattr(worker,"get_settings",lambda:SimpleNamespace(instagram_ingress_provider="meta"))
    response = SimpleNamespace(ok=True,status_code=200,error=None,model_dump=lambda:{"ok":True})
    send = AsyncMock(return_value=response)
    meta_send = AsyncMock(side_effect=AssertionError("wrong provider"))
    monkeypatch.setattr(brevo,"send_brevo_reply",send)
    monkeypatch.setattr(meta,"send_meta_instagram_reply",meta_send)
    await worker._send_reply(IncomingMessage(provider="brevo",channel="instagram",text="fake"),AgentResult(reply_text="ok",intent="commerce"))
    send.assert_awaited_once()
    meta_send.assert_not_awaited()


def test_inbox_claim_only_selects_first_unfinished_turn_per_conversation(monkeypatch):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cursor
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(inbox,"get_settings",lambda:SimpleNamespace(database_url="fake"))
    monkeypatch.setattr(inbox,"ensure_tables",lambda:None)
    monkeypatch.setattr(inbox,"get_conn",connection)
    inbox.claim_pending_inbox()
    sql = cursor.execute.call_args.args[0]
    # Execute the actual selection predicate in an isolated SQLite database;
    # only PostgreSQL locking/parameter syntax is removed, never the predicates.
    selection = sql[sql.index("SELECT candidate.id"):sql.index("FOR UPDATE SKIP LOCKED")]
    selection = selection.replace("public.ai_inbound_inbox","queue")
    db = sqlite3.connect(":memory:")
    db.create_function("now",0,lambda:100)
    db.execute("CREATE TABLE queue(id,conversation_key,sender_key,provider,channel,status,attempts,max_attempts,lease_expires_at,created_at)")
    db.executemany("INSERT INTO queue VALUES(?,?,?,?,?,?,?,?,?,?)",[
        (1,'A','sender-A','brevo','whatsapp','leased',1,8,200,1),
        (2,'A','sender-A','brevo','whatsapp','pending',0,8,None,2),
        (3,'B','sender-B','brevo','whatsapp','pending',0,8,None,3),
        (4,'B','sender-B','brevo','whatsapp','pending',0,8,None,4),
    ])
    assert db.execute(selection).fetchall() == [(3,)]
    db.execute("UPDATE queue SET status='processed' WHERE id=1")
    assert db.execute(selection).fetchall() == [(2,),(3,)]
    db.close()
