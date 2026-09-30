from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from app.models import IncomingMessage
from app.channels.instagram_media_reference import is_instagram_publication_url


@pytest.mark.parametrize("url,expected", [
    ("https://www.instagram.com/p/Dd3wsN7Efsb/?img_index=1", True),
    ("https://instagram.com/reel/ABC/", True),
    ("https://instagram.com/stories/loja/123/", True),
    ("https://instagram.com.evil.test/p/ABC/", False),
    ("https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=123", False),
    ("https://instagram.com/loja/", False),
])
def test_publication_is_not_cdn_or_profile(url, expected):
    assert is_instagram_publication_url(url) is expected


@pytest.mark.asyncio
async def test_gustavo_link_cannot_inherit_tissot_or_reach_interpreter(monkeypatch):
    from app.agents import door
    monkeypatch.setattr("app.agents.door_gates.try_entry_gates", lambda message: None)
    interpreter = AsyncMock(side_effect=AssertionError("unresolved link reached catalog"))
    monkeypatch.setattr(door, "interpret_message", interpreter)
    result = await door._generate_agent_reply_async_inner(
        IncomingMessage(channel="instagram", text="https://www.instagram.com/p/Dd3wsN7Efsb/?img_index=1"),
        {"_commerce_state": {"last_presented_products": [{"product_id": "641", "name": "Tissot Seastar"}]}},
    )
    assert result.safety_reason == "instagram_media_unviewable"
    assert "print" in result.reply_text
    assert "Tissot" not in result.reply_text
    interpreter.assert_not_awaited()


@pytest.mark.parametrize("payload", [{}, {"url": "https://www.instagram.com/p/ABC/"}])
def test_shared_post_without_image_is_not_dropped(monkeypatch, payload):
    from app.channels import meta_instagram as adapter
    monkeypatch.setattr(adapter, "_lookup_ig_profile", lambda *args: {})
    messages = adapter.parse_meta_instagram_messaging({"entry": [{"messaging": [{
        "sender": {"id": "123"}, "recipient": {"id": "456"},
        "message": {"mid": "share-1", "attachments": [{"type": "share", "payload": payload}]},
    }]}]})
    assert len(messages) == 1
    assert messages[0].image_url is None
    assert messages[0].channel_metadata["instagram_media_unresolved"] is True


@pytest.mark.asyncio
async def test_video_never_reaches_still_image_vision(monkeypatch):
    from app.agents.door_media import try_media_routes
    from app.catalog.vision import identify, image_product_id
    photo = AsyncMock(side_effect=AssertionError("video sent as image"))
    monkeypatch.setattr("app.agents.door.handle_image_product_search", photo)
    message = IncomingMessage(channel="instagram", text="Esse?", image_url="https://example.com/video.mp4",
                              attachment_type="video", input_modality="text_with_image")
    assert not identify.image_search_eligible(message)
    assert not image_product_id.image_search_eligible(message)
    result = await try_media_routes(message, None)
    assert result.safety_reason == "instagram_media_unviewable"
    photo.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status", ["pending", "ambiguous"])
async def test_opaque_story_video_is_decoded_even_when_labeled_image(monkeypatch, prior_status):
    from app.stories import instagram_story_service as service
    from app.stories.instagram_story_models import InstagramStoryContext, StoryProductAssociation
    pending = StoryProductAssociation(tenant_id="test", provider="meta", instagram_account_id="biz",
                                      story_media_id="story-1", match_status=prior_status,
                                      media_mime="video/mp4", visual_analysis={"media_type": "image"})
    repo = Mock()
    repo.get_by_story.return_value = pending
    repo.begin_processing.return_value = pending
    repo.find_by_media_hash.return_value = None
    repo.find_visual_analysis_by_hash.return_value = None
    monkeypatch.setattr(service, "StoryProductRepository", lambda: repo)
    monkeypatch.setattr(service, "resolve_story_tenant", AsyncMock(return_value=SimpleNamespace(
        ok=True, tenant_id="test", source="test")))
    monkeypatch.setattr(service, "story_rollout_allows", lambda **kw: (True, "full"))
    monkeypatch.setattr(service, "_hydrate_story_media", AsyncMock(side_effect=lambda story: story))
    monkeypatch.setattr(service, "get_cached_visual_analysis", lambda **kw: None)
    monkeypatch.setattr(service, "download_story_media", AsyncMock(return_value=SimpleNamespace(
        content=b"video-bytes", content_type="video/mp4", sha256="testhash", storage_path=None,
        byte_count=11, final_host="lookaside.fbsbx.com")))
    decode = Mock(return_value=[])
    monkeypatch.setattr(service, "extract_video_frames_best_effort", decode)
    vision = AsyncMock(side_effect=AssertionError("raw video reached image model"))
    monkeypatch.setattr(service, "analyze_story_image", vision)
    message = IncomingMessage(channel="instagram", text="Quanto custa esse?", instagram_story=InstagramStoryContext(
        provider="meta", instagram_account_id="biz", story_media_id="story-1", replied_to_story=True,
        media_type="image", story_media_url_private=SecretStr("https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=123")))
    result = await service.resolve_story_product_question(incoming=message, execute_tool=AsyncMock())
    decode.assert_called_once()
    assert result.failure_reason == "video_decoder_unavailable"
    vision.assert_not_awaited()


@pytest.mark.asyncio
async def test_story_analysis_sends_all_five_decoded_frames(monkeypatch):
    from app.stories import story_visual_analyzer as visual
    from app.stories.instagram_story_models import StoryVisualUnderstanding
    parse = AsyncMock(return_value=SimpleNamespace(parsed=StoryVisualUnderstanding(), metrics=None))
    monkeypatch.setattr(visual, "parse_structured_output", parse)
    await visual.analyze_story_image(image_bytes=b"first", extra_frame_bytes=[b"2", b"3", b"4", b"5"], media_type="video")
    parts = parse.call_args.kwargs["messages"][1]["content"]
    assert len([p for p in parts if p["type"] == "image_url"]) == 5
    with pytest.raises(ValueError, match="decoded_image"):
        await visual.analyze_story_image(image_bytes=b"mp4", content_type="video/mp4")
