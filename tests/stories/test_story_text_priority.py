from unittest.mock import AsyncMock
import pytest
from app.models import IncomingMessage
from app.stories.instagram_story_models import InstagramStoryContext, StoryVisualUnderstanding, VisualProductRegion
from app.stories.instagram_story_intent import (
    should_route_story_question, should_silence_story_feedback,
    story_requires_text_first,
)
from app.stories.instagram_story_service import _clarification_from_regions


def incoming(text):
    return IncomingMessage(text=text, instagram_story=InstagramStoryContext(
        replied_to_story=True, story_media_id="story-test"))


@pytest.mark.parametrize("text", [
    "Parabéns! Encontrar vocês foi um achado, indo para meu terceiro relógio!",
    "Ansioso, pedidos do dia 11/09, previsão de envio?",
    "Preciso da garantia do meu relógio",
    "Felipe, qual o tamanho do seu pulso?",
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


@pytest.mark.parametrize("text", [
    "Quero um", "Quanto custa esse?", "Obrigado! Quanto custa esse?",
    "Tem esse?", "Qual o modelo?", "Qual o tamanho desse?",
    "Muito legal, qual esse relógio?", "Que relógio é esse?",
])
def test_product_requests_keep_story_resolution(text):
    assert should_route_story_question(incoming(text))


@pytest.mark.parametrize("text", [
    "top, os envios!", "top o envio!", "Lindo!", "Amei", "Show demais", "🔥", "👏👏",
])
def test_social_story_feedback_is_recorded_without_auto_reply(text):
    message = incoming(text)
    assert should_silence_story_feedback(message)
    assert story_requires_text_first(message)
    assert not should_route_story_question(message)


@pytest.mark.parametrize("text", [
    "Qual o tamanho do seu pulso?", "Meu pedido não chegou", "Preciso de ajuda",
    "Quando vocês enviam?", "Tem previsão de envio?", "Qual o valor da entrega?", "Bom dia",
])
def test_non_product_story_text_uses_normal_agent_without_being_silenced(text):
    message = incoming(text)
    assert story_requires_text_first(message)
    assert not should_route_story_question(message)
    assert not should_silence_story_feedback(message)


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
