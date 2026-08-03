import pytest
from httpx import ASGITransport, AsyncClient

from app.models import AgentResult, BrevoSendResult
from app.webhook_parser import webhook_event_skip_reason


def _fragment_payload() -> dict:
    return {
        "eventName": "conversationFragment",
        "conversationId": "conversation-1",
        "messages": [
            {
                "type": "visitor",
                "id": "message-1",
                "text": "mensagem inbound",
            }
        ],
        "visitor": {
            "id": "visitor-1",
            "attributes": {"SMS": "5511999999999"},
        },
    }


async def _post_webhook(index, payload):
    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=index.app),
            base_url="http://test",
        ) as client:
            return await client.post(
                "/api/webhooks/brevo/whatsapp",
                json=payload,
            )
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)


@pytest.mark.asyncio
async def test_conversation_fragment_enters_agent_pipeline(monkeypatch, capsys):
    import api.index as index

    processed = []

    async def process(incoming, customer_context):
        processed.append((incoming, customer_context))
        return AgentResult(reply_text="ok", intent="commerce")

    async def send(*_args):
        return BrevoSendResult(
            ok=True,
            dry_run=True,
            provider_response={"accepted": True},
        )

    monkeypatch.setattr(index, "inbound_message_exists", lambda *_args: False)
    monkeypatch.setattr(index, "claim_inbound_message", lambda _message: (True, 101))
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *_args: True)
    monkeypatch.setattr(index, "find_customer_profile_by_phone", lambda _phone: {})
    monkeypatch.setattr(index, "process_incoming_message", process)
    monkeypatch.setattr(index, "send_brevo_reply", send)
    monkeypatch.setattr(index, "insert_agent_response", lambda _data: None)

    response = await _post_webhook(index, _fragment_payload())

    assert response.status_code == 200
    assert len(processed) == 1
    assert processed[0][0].event_type == "conversationFragment"
    output = capsys.readouterr().out
    assert "[brevo.webhook] routing" in output
    assert "'event_name': 'conversationFragment'" in output
    assert "'should_process': True" in output
    assert "[brevo.webhook] processing" in output


@pytest.mark.asyncio
async def test_conversation_transcript_is_explicitly_ignored(monkeypatch, capsys):
    import api.index as index

    payload = {
        **_fragment_payload(),
        "eventName": "conversationTranscript",
    }
    monkeypatch.setattr(
        index,
        "claim_inbound_message",
        lambda _message: (_ for _ in ()).throw(
            AssertionError("transcript must not be claimed")
        ),
    )
    monkeypatch.setattr(
        index,
        "process_incoming_message",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("transcript must not be processed")
        ),
    )

    response = await _post_webhook(index, payload)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "skipped": True,
        "reason": "non_inbound_event",
    }
    output = capsys.readouterr().out
    assert "'event_name': 'conversationTranscript'" in output
    assert "'should_process': False" in output
    assert "'reason': 'non_inbound_event'" in output
    assert "[brevo.webhook] processing" not in output


@pytest.mark.asyncio
async def test_conversation_started_without_message_preserves_no_text_skip(
    monkeypatch,
    capsys,
):
    import api.index as index

    monkeypatch.setattr(
        index,
        "process_incoming_message",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("empty conversationStarted must not be processed")
        ),
    )
    payload = {
        "eventName": "conversationStarted",
        "conversationId": "conversation-1",
        "visitor": {
            "id": "visitor-1",
            "attributes": {"SMS": "5511999999999"},
        },
    }

    response = await _post_webhook(index, payload)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "skipped": True,
        "reason": "no_text",
    }
    output = capsys.readouterr().out
    assert "'event_name': 'conversationStarted'" in output
    assert "'should_process': False" in output
    assert "'reason': 'no_text'" in output


@pytest.mark.asyncio
async def test_image_only_fragment_is_not_skipped_as_no_text(monkeypatch):
    import api.index as index

    processed = []

    async def process(incoming, customer_context):
        processed.append(incoming)
        return AgentResult(reply_text="ok", intent="commerce")

    async def send(*_args):
        return BrevoSendResult(
            ok=True,
            dry_run=True,
            provider_response={"accepted": True},
        )

    monkeypatch.setattr(index, "inbound_message_exists", lambda *_args: False)
    monkeypatch.setattr(index, "claim_inbound_message", lambda _message: (True, 202))
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *_args: True)
    monkeypatch.setattr(index, "find_customer_profile_by_phone", lambda _phone: {})
    monkeypatch.setattr(index, "process_incoming_message", process)
    monkeypatch.setattr(index, "send_brevo_reply", send)
    monkeypatch.setattr(index, "insert_agent_response", lambda _data: None)

    payload = {
        "eventName": "conversationFragment",
        "conversationId": "conversation-img",
        "messages": [
            {
                "type": "visitor",
                "id": "message-img-1",
                "file": {
                    "link": "https://cdn.example.com/watch.jpg",
                    "mimeType": "image/jpeg",
                    "type": "image",
                    "name": "watch.jpg",
                },
            }
        ],
        "visitor": {
            "id": "visitor-1",
            "attributes": {"WHATSAPP": "5511999999999"},
        },
    }

    response = await _post_webhook(index, payload)

    assert response.status_code == 200
    assert len(processed) == 1
    assert processed[0].image_url == "https://cdn.example.com/watch.jpg"
    assert processed[0].attachment_type == "image"


@pytest.mark.asyncio
async def test_conversation_busy_returns_200_skip_not_503(monkeypatch):
    import api.index as index
    from app.conversation_lock import ConversationLockUnavailable

    async def busy_lock(*_args, **_kwargs):
        raise ConversationLockUnavailable("database_lock_unavailable")

    monkeypatch.setattr(index, "inbound_message_exists", lambda *_args: False)
    monkeypatch.setattr(index, "acquire_conversation_lock", busy_lock)
    monkeypatch.setattr(
        index,
        "claim_inbound_message",
        lambda _message: (_ for _ in ()).throw(
            AssertionError("busy conversation must not claim")
        ),
    )
    monkeypatch.setattr(
        index,
        "process_incoming_message",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("busy conversation must not process")
        ),
    )

    response = await _post_webhook(index, _fragment_payload())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "skipped": True,
        "reason": "conversation_busy",
    }


def test_event_skip_reason_only_blocks_transcript_event():
    assert webhook_event_skip_reason({
        "eventName": "conversationTranscript",
    }) == "non_inbound_event"
    assert webhook_event_skip_reason({
        "eventName": "conversationFragment",
    }) is None
    assert webhook_event_skip_reason({
        "eventName": "conversationStarted",
    }) is None
