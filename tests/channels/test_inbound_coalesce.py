from app.db import _prepare_inbound_message
from app.channels.inbound_coalesce import (
    attach_recent_image_for_followup,
    is_caption_echo,
    is_caption_echo_of_recent_image,
    normalize_caption_text,
)
from app.models import IncomingMessage


def test_normalize_caption_collapses_and_lowercases():
    assert normalize_caption_text("  E Esse?  ") == "e esse?"


def test_normalize_caption_placeholder_is_empty():
    assert normalize_caption_text("[Imagem recebida via WhatsApp]") == ""
    assert normalize_caption_text("[Sticker recebido via WhatsApp]") == ""


def test_is_caption_echo_exact_match():
    incoming = IncomingMessage(
        channel="whatsapp",
        text="e esse?",
        conversation_id="c1",
    )
    recent = {"id": 1, "text": "E Esse?", "channel_metadata": {"image_url": "https://x/a.jpg"}}
    assert is_caption_echo(incoming, recent) is True


def test_is_caption_echo_rejects_image_inbound():
    incoming = IncomingMessage(
        channel="whatsapp",
        text="e esse?",
        image_url="https://x/a.jpg",
        conversation_id="c1",
    )
    recent = {"id": 1, "text": "e esse?", "channel_metadata": {"image_url": "https://x/b.jpg"}}
    assert is_caption_echo(incoming, recent) is False


def test_is_caption_echo_rejects_empty_photo_caption():
    """Photo without caption must not suppress a later real text turn."""
    incoming = IncomingMessage(
        channel="whatsapp",
        text="e esse?",
        conversation_id="c1",
    )
    recent = {
        "id": 1,
        "text": "[Imagem recebida via WhatsApp]",
        "channel_metadata": {"image_url_present": True},
    }
    assert is_caption_echo(incoming, recent) is False


def test_is_caption_echo_rejects_different_text():
    incoming = IncomingMessage(
        channel="whatsapp",
        text="quanto custa?",
        conversation_id="c1",
    )
    recent = {"id": 1, "text": "e esse?", "channel_metadata": {"image_url": "https://x/a.jpg"}}
    assert is_caption_echo(incoming, recent) is False


def test_prepare_inbound_persists_image_url_in_metadata():
    prepared = _prepare_inbound_message(
        {
            "provider": "brevo",
            "text": "e esse?",
            "image_url": "https://example.com/watch.jpg",
            "input_modality": "text_with_image",
            "attachment_type": "image",
            "channel_metadata": {},
        }
    )
    metadata = prepared["channel_metadata"].obj
    assert metadata["image_url_present"] is True
    assert metadata["image_url"] == "https://example.com/watch.jpg"
    assert metadata["input_modality"] == "text_with_image"
    assert metadata["attachment_type"] == "image"


def test_is_caption_echo_of_recent_image_short_circuits_without_db(monkeypatch):
    from app.channels import inbound_coalesce as module

    monkeypatch.setattr(
        module,
        "recent_image_inbound_for_echo",
        lambda **_kwargs: {
            "id": 9,
            "text": "e esse?",
            "channel_metadata": {"image_url": "https://x/a.jpg"},
        },
    )
    incoming = IncomingMessage(channel="whatsapp", text="e esse?", conversation_id="c1")
    assert is_caption_echo_of_recent_image(incoming) is True


def test_attach_recent_image_for_whatsapp_valor(monkeypatch):
    from app.channels import inbound_coalesce as module

    monkeypatch.setattr(
        module,
        "recent_image_inbound_for_echo",
        lambda **kwargs: {
            "id": 77,
            "text": "[Imagem recebida via WhatsApp]",
            "channel_metadata": {
                "image_url": "https://cdn.example.com/watch.jpg",
                "image_url_present": True,
            },
        },
    )
    incoming = IncomingMessage(
        channel="whatsapp",
        text="valor",
        conversation_id="c1",
        sender_key="whatsapp:5511999999999",
    )
    updated = attach_recent_image_for_followup(incoming)
    assert updated.image_url == "https://cdn.example.com/watch.jpg"
    assert updated.input_modality == "text_with_image"
    assert updated.channel_metadata["attached_from_recent_inbound_id"] == 77


def test_channel_exception_handlers_are_not_bare_pass():
    import ast
    from pathlib import Path

    silent: list[str] = []
    for path in Path("app/channels").rglob("*.py"):
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
