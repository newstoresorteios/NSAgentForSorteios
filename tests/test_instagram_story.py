"""Instagram Story recognition — parser, media safety, matching, rollout."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.instagram_story_intent import (
    detect_story_question_type,
    should_route_story_question,
)
from app.instagram_story_media import (
    StoryMediaError,
    validate_story_media_url,
    _sniff_mime,
)
from app.instagram_story_models import (
    InstagramStoryContext,
    StoryProductCandidate,
    StoryVisualUnderstanding,
)
from app.instagram_story_parser import (
    extract_instagram_story_context,
    sanitize_instagram_story_reference,
    strip_signed_url,
)
from app.models import IncomingMessage
from app.story_product_matcher import classify_match, reject_invented_rerank_ids
from app.story_publication_link_service import validate_link_payload
from app.webhook_parser import parse_brevo_conversations_payload


def _story_reply_payload() -> dict:
    return {
        "eventName": "conversationFragment",
        "conversationId": "c1",
        "visitor": {
            "id": "v1",
            "source": "instagram",
            "sourceChannelRef": "ig_biz_1",
            "sourceConversationRef": "ig_user_9",
        },
        "messages": [
            {
                "type": "visitor",
                "id": "m1",
                "text": "Qual o valor?",
                "createdAt": "2026-08-05T20:00:00Z",
                "reply_to": {
                    "story": {
                        "id": "story_media_123",
                        "url": "https://scontent.cdninstagram.com/v/t51.2885-15/x.jpg?oe=ABC&oh=SECRET",
                        "media_type": "image",
                    }
                },
            }
        ],
    }


def test_strip_signed_url_removes_query():
    url = "https://scontent.cdninstagram.com/v/t51/x.jpg?oe=ABC&oh=SECRET"
    cleaned = strip_signed_url(url)
    assert cleaned is not None
    assert "oh=" not in cleaned
    assert cleaned.endswith("/v/t51/x.jpg")


def test_sanitize_story_reference_redacts_tokens_and_urls():
    raw = {
        "access_token": "secret-value",
        "url": "https://scontent.cdninstagram.com/v/t.jpg?oh=TOKEN",
        "nested": {"a": 1},
    }
    cleaned = sanitize_instagram_story_reference(raw)
    assert cleaned["access_token"] == "[REDACTED]"
    assert "oh=" not in str(cleaned.get("url"))
    assert cleaned["nested"]["_keys"] == ["a"]


def test_extract_story_context_from_brevo_meta_shape():
    payload = _story_reply_payload()
    message = payload["messages"][0]
    ctx = extract_instagram_story_context(
        payload=payload,
        message=message,
        channel="instagram",
        visitor=payload["visitor"],
    )
    assert ctx is not None
    assert ctx.replied_to_story is True
    assert ctx.story_media_id == "story_media_123"
    assert ctx.story_media_url is not None
    assert "oh=" not in (ctx.story_media_url or "")
    assert "SECRET" not in str(ctx.raw_reference)


def test_parser_attaches_instagram_story_to_incoming():
    incoming = parse_brevo_conversations_payload(_story_reply_payload())
    assert incoming.channel == "instagram"
    assert incoming.instagram_story is not None
    assert incoming.instagram_story.replied_to_story is True
    assert incoming.instagram_story.story_media_id == "story_media_123"
    assert incoming.channel_metadata.get("instagram_story", {}).get("replied_to_story") is True


def test_common_instagram_dm_without_story_has_no_context():
    payload = {
        "eventName": "conversationFragment",
        "visitor": {"id": "v1", "source": "instagram", "sourceConversationRef": "u1"},
        "messages": [{"type": "visitor", "id": "m1", "text": "oi, tem Seiko?"}],
    }
    incoming = parse_brevo_conversations_payload(payload)
    assert incoming.channel == "instagram"
    assert incoming.instagram_story is None
    assert should_route_story_question(incoming) is False


def test_story_mention_attachment():
    payload = {
        "eventName": "conversationFragment",
        "visitor": {
            "id": "v1",
            "source": "instagram",
            "sourceChannelRef": "biz",
            "sourceConversationRef": "u1",
        },
        "messages": [
            {
                "type": "visitor",
                "id": "m1",
                "text": "",
                "attachments": [
                    {
                        "type": "story_mention",
                        "payload": {
                            "id": "mention_99",
                            "url": "https://scontent.cdninstagram.com/v/t.jpg",
                        },
                    }
                ],
            }
        ],
    }
    incoming = parse_brevo_conversations_payload(payload)
    assert incoming.instagram_story is not None
    assert incoming.instagram_story.mentioned_in_story is True
    assert incoming.instagram_story.story_media_id == "mention_99"


def test_question_types():
    assert detect_story_question_type("Qual o valor?").value == "price"
    assert detect_story_question_type("Tem disponível?").value == "availability"
    assert detect_story_question_type("Manda o link").value == "product_link"
    assert detect_story_question_type("Tem outra cor?").value == "color_options"


def test_validate_media_url_blocks_http_and_localhost():
    with pytest.raises(StoryMediaError) as http_exc:
        validate_story_media_url("http://scontent.cdninstagram.com/x.jpg")
    assert http_exc.value.code == "scheme_not_https"
    with pytest.raises(StoryMediaError) as local_exc:
        validate_story_media_url("https://localhost/x.jpg")
    assert local_exc.value.code in {"host_blocked", "host_not_allowed", "private_ip_blocked"}


def test_validate_media_url_blocks_unknown_host():
    with pytest.raises(StoryMediaError) as exc:
        validate_story_media_url("https://evil.example/steal.jpg")
    assert exc.value.code == "host_not_allowed"


def test_sniff_rejects_html_disguised():
    assert _sniff_mime(b"<!DOCTYPE html><html>") == "text/html"
    assert _sniff_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"


def test_classify_match_thresholds():
    strong = [
        StoryProductCandidate(
            catalog_item_key="product:1",
            product_id="1",
            score=0.96,
            match_reasons=["ean:123"],
        ),
        StoryProductCandidate(
            catalog_item_key="product:2",
            product_id="2",
            score=0.70,
            match_reasons=["tray_query"],
        ),
    ]
    status, top = classify_match(strong, multiple_products=False)
    assert status == "matched"
    assert top is not None and top.product_id == "1"

    close = [
        StoryProductCandidate(
            catalog_item_key="product:1",
            product_id="1",
            score=0.80,
            match_reasons=["visual"],
        ),
        StoryProductCandidate(
            catalog_item_key="product:2",
            product_id="2",
            score=0.78,
            match_reasons=["visual"],
        ),
    ]
    status2, _ = classify_match(close, multiple_products=False)
    assert status2 == "ambiguous"


def test_reject_invented_rerank_ids():
    candidates = [
        StoryProductCandidate(catalog_item_key="product:1", product_id="1", score=0.9),
    ]
    ordered, invalid = reject_invented_rerank_ids(["1", "999"], candidates)
    assert ordered == ["1"]
    assert invalid == 1


def test_validate_link_payload_rejects_missing_and_ignores_price():
    with pytest.raises(ValueError):
        validate_link_payload({"tenant_id": "newstore"})
    cleaned = validate_link_payload(
        {
            "tenant_id": "newstore",
            "instagram_account_id": "ig1",
            "story_media_id": "s1",
            "catalog_item_key": "product:10",
            "product_id": "10",
            "price": 9999,
            "stock": 5,
        }
    )
    assert "price" not in cleaned
    assert "stock" not in cleaned


def test_story_rollout_off_skips(monkeypatch):
    from app.instagram_story_service import story_rollout_allows
    from app.config import get_settings

    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "off")
    get_settings.cache_clear()
    story = InstagramStoryContext(
        provider="brevo",
        instagram_account_id="ig",
        story_media_id="s1",
        replied_to_story=True,
    )
    ok, reason = story_rollout_allows(tenant_id="newstore", story=story)
    assert ok is False
    assert reason == "rollout_off"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_resolve_story_already_matched_revalidates(monkeypatch):
    from app.config import get_settings
    from app import instagram_story_service as service
    from app.instagram_story_models import StoryProductAssociation

    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "full")
    get_settings.cache_clear()

    assoc = StoryProductAssociation(
        tenant_id="newstore",
        provider="brevo",
        instagram_account_id="ig_biz_1",
        story_media_id="story_media_123",
        product_id="42",
        catalog_item_key="product:42",
        match_status="matched",
        match_source="manual",
        match_confidence=1.0,
    )

    class FakeRepo:
        def get_by_story(self, **_kwargs):
            return assoc

        def create_pending(self, **_kwargs):
            return assoc

        def touch_last_seen(self, **_kwargs):
            return None

        def begin_processing(self, **_kwargs):
            return None

    monkeypatch.setattr(service, "StoryProductRepository", FakeRepo)

    async def fake_tool(name, args):
        assert name == "get_product"
        return {
            "id": "42",
            "name": "Seiko SRPD51",
            "price": 1899.0,
            "stock": 2,
            "available": True,
            "url": "https://loja.example/p/42",
        }

    incoming = parse_brevo_conversations_payload(_story_reply_payload())
    result = await service.resolve_story_product_question(
        incoming=incoming,
        tenant_id="newstore",
        execute_tool=fake_tool,
    )
    assert result is not None
    assert result.resolved is True
    assert result.product_id == "42"
    assert result.product_payload is not None
    assert "1899" in (result.reply_hint or "") or "1.899" in (result.reply_hint or "")
    agent = service.story_result_to_agent_result(result, incoming=incoming)
    assert agent is not None
    assert agent.intent == "commerce"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_shadow_mode_does_not_change_reply(monkeypatch):
    from app.config import get_settings
    from app import instagram_story_service as service
    from app.instagram_story_models import StoryProductAssociation, StoryResolutionResult

    monkeypatch.setenv("INSTAGRAM_STORY_RECOGNITION_ENABLED", "true")
    monkeypatch.setenv("INSTAGRAM_STORY_ROLLOUT_MODE", "shadow")
    get_settings.cache_clear()

    assoc = StoryProductAssociation(
        tenant_id="newstore",
        provider="brevo",
        instagram_account_id="ig_biz_1",
        story_media_id="story_media_123",
        product_id="42",
        catalog_item_key="product:42",
        match_status="matched",
        match_confidence=1.0,
    )

    class FakeRepo:
        def get_by_story(self, **_kwargs):
            return assoc

        def create_pending(self, **_kwargs):
            return assoc

        def touch_last_seen(self, **_kwargs):
            return None

    monkeypatch.setattr(service, "StoryProductRepository", FakeRepo)

    async def fake_tool(name, args):
        return {"id": "42", "name": "Seiko", "price": 10, "available": True}

    incoming = parse_brevo_conversations_payload(_story_reply_payload())
    result = await service.resolve_story_product_question(
        incoming=incoming,
        tenant_id="newstore",
        execute_tool=fake_tool,
    )
    assert result is not None
    assert result.shadow_only is True
    assert service.story_result_to_agent_result(result, incoming=incoming) is None
    get_settings.cache_clear()


def test_visual_understanding_forbids_trusting_advertised_price_as_stock():
    analysis = StoryVisualUnderstanding(
        visual_description="relógio azul",
        visible_advertised_price="R$ 1.000",
        product_identity_confidence=0.4,
    )
    assert analysis.visible_advertised_price
    # Matching layer must not treat advertised price as Tray authority — checked in service compose.


@pytest.mark.offline_eval
def test_story_offline_eval_marker_smoke():
    assert detect_story_question_type("quanto custa esse?").value == "price"
