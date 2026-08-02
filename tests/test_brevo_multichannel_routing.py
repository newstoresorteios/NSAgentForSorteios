import pytest
from httpx import ASGITransport, AsyncClient

from app.models import AgentResult, BrevoSendResult


async def _post(index, payload):
    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=index.app),
            base_url="http://test",
        ) as client:
            return await client.post("/api/webhooks/brevo/conversations", json=payload)
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)


@pytest.mark.asyncio
async def test_instagram_without_phone_runs_pipeline_and_replies_to_visitor(monkeypatch):
    import api.index as index

    persisted = []
    processed = []
    sent = []

    monkeypatch.setattr(index, "inbound_message_exists", lambda *_: False)
    monkeypatch.setattr(
        index,
        "claim_inbound_message",
        lambda message: (persisted.append(dict(message)) or True, 91),
    )
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *_: True)
    monkeypatch.setattr(
        index,
        "find_customer_profile_by_phone",
        lambda _phone: (_ for _ in ()).throw(AssertionError("social identity is not a phone")),
    )

    async def process(incoming, customer_context):
        processed.append((incoming, customer_context))
        return AgentResult(reply_text="Encontrei opções oficiais.", intent="commerce")

    async def send(incoming, result):
        sent.append((incoming, result))
        return BrevoSendResult(ok=True, dry_run=False, status_code=201)

    monkeypatch.setattr(index, "process_incoming_message", process)
    monkeypatch.setattr(index, "send_brevo_reply", send)
    monkeypatch.setattr(index, "insert_agent_response", lambda _data: None)

    response = await _post(index, {
        "eventName": "conversationStarted",
        "conversationId": "conv-instagram-001",
        "message": {
            "id": "msg-instagram-001",
            "type": "visitor",
            "text": "Vocês têm relógio Tissot?",
            "createdAt": 1785700000000,
        },
        "visitor": {
            "id": "brevo-visitor-instagram-001",
            "source": "instagram",
            "sourceConversationRef": "instagram-user-999",
            "displayedName": "Cliente Instagram",
        },
    })

    assert response.status_code == 200
    assert response.json()["reply_sent"] is True
    assert persisted[0]["channel"] == "instagram"
    assert persisted[0]["sender_phone"] is None
    assert processed[0][1] == {
        "found": False,
        "channel": "instagram",
        "sender_key": "instagram:instagram-user-999",
        "display_name": "Cliente Instagram",
    }
    assert sent[0][0].visitor_id == "brevo-visitor-instagram-001"


@pytest.mark.asyncio
async def test_own_agent_message_is_skipped_without_response(monkeypatch):
    import api.index as index

    monkeypatch.setattr(
        index,
        "claim_inbound_message",
        lambda *_: (_ for _ in ()).throw(AssertionError("agent message must not be claimed")),
    )
    monkeypatch.setattr(
        index,
        "send_brevo_reply",
        lambda *_: (_ for _ in ()).throw(AssertionError("agent message must not be answered")),
    )

    response = await _post(index, {
        "eventName": "conversationFragment",
        "conversationId": "conv-fb",
        "messages": [{
            "id": "agent-1",
            "type": "agent",
            "receivedFrom": "NewStoreAgent",
            "text": "Resposta enviada",
        }],
        "visitor": {"id": "visitor-fb", "source": "facebook"},
    })

    assert response.json() == {"ok": True, "skipped": True, "reason": "agent_message"}
