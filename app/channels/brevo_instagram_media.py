"""Brevo Instagram media limitations helpers.

Instagram Story replies / some IG media arrive in Brevo Conversations as a
placeholder text without an attachment URL:

  "This message cannot be viewed in Brevo. Please go to Instagram app to view it."

Without a media URL, Story recognition and image product search cannot run.
These helpers detect that case and provide a clear visitor-facing guide.
"""

from __future__ import annotations

from app.configuration.runtime import message as operator_message

from app.models import IncomingMessage

_UNVIEWABLE_MARKERS = (
    "this message cannot be viewed in brevo",
    "cannot be viewed in brevo",
    "please go to instagram app to view it",
    "mensagem não pode ser visualizada no brevo",
    "mensagem nao pode ser visualizada no brevo",
)

def UNVIEWABLE_MEDIA_GUIDE_REPLY():
    return operator_message('instructions.unviewable_media_guide_reply.38b7fbef16')

def PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY():
    return operator_message('instructions.price_without_image_instagram_reply.d4742da0f8')


def is_brevo_unviewable_media_text(text: str | None) -> bool:
    """True when Brevo replaced Instagram media with an unviewable placeholder."""
    normalized = " ".join(str(text or "").casefold().split())
    if not normalized:
        return False
    return any(marker in normalized for marker in _UNVIEWABLE_MARKERS)


def is_bare_price_request(text: str | None) -> bool:
    """Short price asks like 'valor' / 'preço' without naming a product."""
    normalized = " ".join(str(text or "").casefold().split()).strip()
    if not normalized:
        return False
    bare = {
        "valor",
        "preço",
        "preco",
        "quanto",
        "quanto custa",
        "quanto é",
        "quanto e",
        "qual o valor",
        "qual valor",
        "qual o preço",
        "qual o preco",
        "qual preço",
        "qual preco",
        "e o valor",
        "e o preço",
        "e o preco",
        "o valor",
        "o preço",
        "o preco",
    }
    return normalized in bare


def should_guide_instagram_price_without_media(message: IncomingMessage) -> bool:
    """Instagram price follow-up with no image and no way to identify the SKU."""
    if (message.channel or "").lower() != "instagram":
        return False
    if (message.image_url or "").strip():
        return False
    if is_brevo_unviewable_media_text(message.text):
        return True
    return is_bare_price_request(message.text)
