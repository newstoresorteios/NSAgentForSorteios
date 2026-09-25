import pytest
from app.ingress.reconstruct import incoming_from_inbox_payload
from app.channels.inbound_coalesce import attach_recent_image_for_followup
from app.models import IncomingMessage


def test_reconstruct_meta_story_message():
    payload = {
        "normalized": {
            "provider": "meta",
            "channel": "instagram",
            "message_id": "m1",
            "text": "valor",
            "image_url": "https://cdn.example/story.jpg",
            "attachment_type": "image",
            "input_modality": "text_with_image",
            "sender_key": "instagram:user-1",
            "sender_external_id": "user-1",
            "visitor_id": "user-1",
            "conversation_id": "ig:user-1",
            "instagram_story": {
                "provider": "meta",
                "instagram_account_id": "17841404241547355",
                "story_media_id": "story-1",
                "replied_to_story": True,
                "mentioned_in_story": False,
                "media_type": "image",
            },
            "raw": {},
        }
    }
    incoming = incoming_from_inbox_payload(payload)
    assert incoming is not None
    assert incoming.provider == "meta"
    assert incoming.image_url == "https://cdn.example/story.jpg"
    assert incoming.instagram_story is not None
    assert incoming.instagram_story.replied_to_story is True
    assert incoming.instagram_story.operational_media_url() == (
        "https://cdn.example/story.jpg"
    )


def test_attach_recent_image_for_valor(monkeypatch):
    from app.channels import inbound_coalesce as module

    monkeypatch.setattr(
        module,
        "recent_image_inbound_for_echo",
        lambda **_kwargs: {
            "id": 42,
            "text": "[Imagem recebida via Instagram]",
            "channel_metadata": {
                "image_url": "https://cdn.example/watch.jpg",
                "image_url_present": True,
            },
        },
    )
    incoming = IncomingMessage(
        channel="instagram",
        provider="meta",
        text="valor",
        conversation_id="ig:user-1",
        sender_key="instagram:user-1",
    )
    updated = attach_recent_image_for_followup(incoming)
    assert updated.image_url == "https://cdn.example/watch.jpg"
    assert updated.attachment_type == "image"
    assert updated.input_modality == "text_with_image"


def test_meta_provider_respects_human_takeover(monkeypatch):
    import asyncio

    from app.ingress import worker as worker_mod

    monkeypatch.setattr(
        worker_mod,
        "incoming_from_inbox_payload",
        lambda *_args, **_kwargs: IncomingMessage(
            provider="meta",
            channel="instagram",
            text="teste",
            sender_key="instagram:user-1",
            sender_external_id="user-1",
            visitor_id="user-1",
            conversation_id="ig:user-1",
        ),
    )
    monkeypatch.setattr(worker_mod, "attach_recent_image_for_followup", lambda incoming: incoming)
    monkeypatch.setattr(
        "app.ops.human_takeover.human_takeover_active",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(worker_mod, "claim_inbound_message", lambda *_args, **_kwargs: (True, 9))
    monkeypatch.setattr(worker_mod, "mark_inbox_processed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_mod, "mark_inbox_failed", lambda *_args, **_kwargs: None)

    result = asyncio.run(
        worker_mod.process_inbox_row({"id": 1, "payload_json": {}, "attempts": 1})
    )
    assert result["ok"] is True
    assert result.get("skipped") == "human_takeover"
    assert result.get("inbound_id") == 9


def test_process_inbox_row_skips_caption_echo(monkeypatch):
    import asyncio

    from app.ingress import worker as worker_mod

    monkeypatch.setattr(
        worker_mod,
        "incoming_from_inbox_payload",
        lambda *_args, **_kwargs: IncomingMessage(
            provider="brevo",
            channel="whatsapp",
            text="foto do relógio",
            sender_key="whatsapp:5511999999999",
            conversation_id="conversation-1",
        ),
    )
    monkeypatch.setattr(worker_mod, "is_caption_echo_of_recent_image", lambda _incoming: True)
    monkeypatch.setattr(
        worker_mod,
        "attach_recent_image_for_followup",
        lambda _incoming: (_ for _ in ()).throw(AssertionError("echo must not attach")),
    )
    monkeypatch.setattr(
        worker_mod,
        "claim_inbound_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("caption echo must not be claimed")
        ),
    )
    marked: list[int] = []
    monkeypatch.setattr(
        worker_mod,
        "mark_inbox_processed",
        lambda inbox_id, **_kwargs: marked.append(inbox_id),
    )

    result = asyncio.run(
        worker_mod.process_inbox_row({"id": 4, "payload_json": {}, "attempts": 1})
    )
    assert result == {"ok": True, "inbox_id": 4, "skipped": "caption_echo"}
    assert marked == [4]


@pytest.mark.parametrize("text", ["top, os envios!", "qual valor?", "Quero comprar", "Meu pedido não chegou", "🔥", ""])
@pytest.mark.parametrize("media_type", ["image", "video"])
def test_story_feedback_is_persisted_but_never_generates_or_sends(monkeypatch, text, media_type):
    import asyncio
    from app.ingress import worker as worker_mod
    from app.stories.instagram_story_models import InstagramStoryContext

    incoming = IncomingMessage(
        provider="meta", channel="instagram", text=text,
        sender_key="instagram:user-1", conversation_id="ig:user-1",
        instagram_story=InstagramStoryContext(replied_to_story=True, story_media_id="s1", media_type=media_type),
    )
    monkeypatch.setattr(worker_mod, "incoming_from_inbox_payload", lambda *_: incoming)
    monkeypatch.setattr(worker_mod, "attach_recent_image_for_followup", lambda item: item)
    monkeypatch.setattr(worker_mod, "claim_inbound_message", lambda *_: (True, 77))
    marked = []
    monkeypatch.setattr(worker_mod, "mark_inbox_processed", lambda row_id, **kw: marked.append((row_id, kw)))

    result = asyncio.run(worker_mod.process_inbox_row({"id": 9, "payload_json": {}, "attempts": 1}))
    assert result == {"ok": True, "inbox_id": 9, "inbound_id": 77, "skipped": "story_message"}
    assert marked == [(9, {"processed_inbound_id": 77})]


def test_plain_dm_is_not_silenced_even_when_text_mentions_story():
    from app.stories.instagram_story_intent import should_silence_story_message
    for text in ("qual valor?", "Vi um relógio no story", "oi"):
        assert not should_silence_story_message(IncomingMessage(channel="instagram", text=text))


def test_instagram_dm_cannot_be_merged_with_story(monkeypatch):
    import asyncio
    from app.ingress import worker
    monkeypatch.setattr(worker, "claim_conversation_burst", lambda *a, **kw: pytest.fail("must not merge Instagram events"))
    incoming = IncomingMessage(channel="instagram", text="oi")
    merged, ids = asyncio.run(worker._claim_and_merge_burst({"id": 12}, incoming))
    assert merged is incoming
    assert ids == [12]
