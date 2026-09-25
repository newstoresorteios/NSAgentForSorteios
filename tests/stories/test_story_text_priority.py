from unittest.mock import AsyncMock
import pytest
from app.models import IncomingMessage
from app.stories.instagram_story_models import InstagramStoryContext, StoryVisualUnderstanding, VisualProductRegion
from app.stories.instagram_story_intent import should_route_story_question
from app.stories.instagram_story_service import _clarification_from_regions


def incoming(text):
    return IncomingMessage(text=text, instagram_story=InstagramStoryContext(
        replied_to_story=True, story_media_id="story-test"))


@pytest.mark.parametrize("text", [
    "Parabéns! Encontrar vocês foi um achado, indo para meu terceiro relógio!",
    "Ansioso, pedidos do dia 11/09, previsão de envio?",
    "Obrigado! Quanto custa esse?",
    "Preciso da garantia do meu relógio",
])
@pytest.mark.asyncio
async def test_text_service_and_social_messages_bypass_all_visual_routes(monkeypatch, text):
    from app.agents.door_media import try_media_routes
    vision = AsyncMock(side_effect=AssertionError("must not analyze Story"))
    monkeypatch.setattr("app.stories.instagram_story_service.resolve_story_product_question", vision)
    monkeypatch.setattr("app.agents.door.handle_image_product_search", vision)
    message = incoming(text)
    message.image_url = "https://example.com/story.jpg"
    assert not should_route_story_question(message)
    assert await try_media_routes(message, None) is None
    vision.assert_not_called()


@pytest.mark.parametrize("text", ["Quero um", "Quanto custa esse?", "Tem esse?", "Qual o modelo?"])
def test_product_requests_keep_story_resolution(text):
    assert should_route_story_question(incoming(text))


def test_many_regions_do_not_leak_internal_labels_or_invent_binary_choice():
    analysis = StoryVisualUnderstanding(watch_count=4, multiple_products=True, product_regions=[
        VisualProductRegion(position="left", label="watch in red-and-black box", dial_color="white"),
        VisualProductRegion(position="left", label="multiple boxes", dial_color="blue"),
    ])
    options, reply = _clarification_from_regions(analysis)
    assert not options
    assert "print" in reply
    assert "left" not in reply and "box" not in reply and "4" not in reply


def test_two_distinct_regions_are_portuguese():
    analysis = StoryVisualUnderstanding(watch_count=2, multiple_products=True, product_regions=[
        VisualProductRegion(position="left", label="watch in box", dial_color="white"),
        VisualProductRegion(position="right", label="watch in box", dial_color="blue"),
    ])
    _, reply = _clarification_from_regions(analysis)
    assert "branco à esquerda" in reply and "azul à direita" in reply
    assert "watch" not in reply
