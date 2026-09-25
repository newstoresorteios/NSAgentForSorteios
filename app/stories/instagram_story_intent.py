"""Detect Story-related customer questions."""

from __future__ import annotations

import re

from app.stories.instagram_story_models import InstagramStoryContext, StoryQuestionType
from app.models import IncomingMessage


_PRICE_RE = re.compile(
    r"\b(valor|pre[cç]o|custa|quanto(?:\s+est[aá])?|qto|\$|r\$)\b",
    re.I,
)
_AVAIL_RE = re.compile(
    r"\b(dispon[ií]vel|tem(?:\s+ainda)?|estoque|pronta\s+entrega|em\s+loja)\b",
    re.I,
)
_LINK_RE = re.compile(r"\b(link|url|site|manda\s+o\s+link|envia\s+o\s+link)\b", re.I)
_COLOR_RE = re.compile(r"\b(outra\s+cor|outras\s+cores|cor\s+diferente|cores?)\b", re.I)
_MODEL_RE = re.compile(
    r"\b(modelo|refer[eê]ncia|qual\s+(?:e|é)\s+(?:esse|este)|que\s+rel[oó]gio)\b",
    re.I,
)
_MECH_RE = re.compile(r"\b(autom[aá]tico|quartz|mecanismo|cron[oó]grafo)\b", re.I)
_SIZE_RE = re.compile(
    r"\b(?:qual|que)\s+(?:o\s+)?tamanho\b|\btamanho\s+(?:desse|deste|do\s+rel[oó]gio)\b",
    re.I,
)
_PURCHASE_RE = re.compile(
    r"\b(?:quero|gostaria\s+de|vou)\s+(?:comprar\s+)?(?:esse|este|um|o)\b|"
    r"\bcomo\s+(?:comprar|fa[cç]o\s+para\s+comprar)\b",
    re.I,
)
_STORY_HINT_RE = re.compile(r"\b(story|storie|stories)\b", re.I)
_SOCIAL_FEEDBACK_RE = re.compile(
    r"\b(?:top|show|lind[oa]|maravilhos[oa]|perfeit[oa]|amei|adorei|"
    r"parab[eé]ns|obrigad[oa]|sensacional|massa|legal|demais)\b",
    re.I,
)
_SERVICE_OR_HELP_RE = re.compile(
    r"\b(?:ajuda|d[uú]vida|pedido|rastreio|rastreamento|envio|entrega|"
    r"troca|devolu[cç][aã]o|garantia|atendimento|problema|n[aã]o\s+chegou)\b",
    re.I,
)
_QUESTION_WORD_RE = re.compile(
    r"\b(?:qual|quais|quanto|quantos|como|quando|onde|por\s+que|porque|tem|pode|"
    r"consegue|voc[eê]s)\b",
    re.I,
)
_SHORT_PRODUCT_DEIXIS = frozenset(
    {"esse", "este", "isso", "valor?", "quanto?", "tem?", "link", "quero um"}
)


def detect_story_question_type(text: str | None) -> StoryQuestionType:
    value = str(text or "").strip()
    if not value:
        return StoryQuestionType.GENERIC
    if _LINK_RE.search(value):
        return StoryQuestionType.PRODUCT_LINK
    if _COLOR_RE.search(value):
        return StoryQuestionType.COLOR_OPTIONS
    if _PRICE_RE.search(value):
        return StoryQuestionType.PRICE
    if _AVAIL_RE.search(value):
        return StoryQuestionType.AVAILABILITY
    if _MECH_RE.search(value) or _MODEL_RE.search(value) or _SIZE_RE.search(value):
        return StoryQuestionType.PRODUCT_DETAILS
    if _STORY_HINT_RE.search(value):
        return StoryQuestionType.PRODUCT_IDENTIFICATION
    # Ultra-short deixis common on Instagram replies.
    if value.casefold() in _SHORT_PRODUCT_DEIXIS:
        return StoryQuestionType.GENERIC
    return StoryQuestionType.GENERIC


def story_has_explicit_product_intent(text: str | None) -> bool:
    """True only when the customer's text asks us to use the Story product."""
    value = str(text or "").strip()
    if not value:
        return False
    if value.casefold() in _SHORT_PRODUCT_DEIXIS:
        return True
    # A Story thumbnail must not hijack support/shipping questions merely
    # because they contain broad words such as "tem" or "valor".
    if _SERVICE_OR_HELP_RE.search(value):
        return False
    if _SIZE_RE.search(value) and re.search(r"\b(?:seu|pulso)\b", value, re.I):
        return False
    return any(pattern.search(value) for pattern in (
        _PRICE_RE, _AVAIL_RE, _LINK_RE, _COLOR_RE, _MODEL_RE, _MECH_RE,
        _SIZE_RE, _PURCHASE_RE, _STORY_HINT_RE,
    ))


def should_silence_story_feedback(incoming: IncomingMessage) -> bool:
    """Keep social acknowledgements in history without sending an automated reply."""
    story = getattr(incoming, "instagram_story", None)
    if not isinstance(story, InstagramStoryContext):
        return False
    text = str(incoming.text or "").strip()
    if not text or story_has_explicit_product_intent(text):
        return False
    if "?" in text or _QUESTION_WORD_RE.search(text):
        return False
    compact = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    emoji_or_punctuation_only = bool(text) and not any(ch.isalnum() for ch in text)
    if _SOCIAL_FEEDBACK_RE.search(text) or emoji_or_punctuation_only or not compact:
        return True
    return False



def story_requires_text_first(incoming: IncomingMessage) -> bool:
    """Non-product text must never be replaced by visual Story analysis.

    The worker handles silent social feedback before the agent. Questions and
    service requests continue through the normal text interpreter.
    """
    story = getattr(incoming, "instagram_story", None)
    if not isinstance(story, InstagramStoryContext):
        return False
    text = incoming.text or ""
    return bool(text.strip()) and not story_has_explicit_product_intent(text)

def should_route_story_question(incoming: IncomingMessage) -> bool:
    story = getattr(incoming, "instagram_story", None)
    if not isinstance(story, InstagramStoryContext):
        return False
    if not (story.replied_to_story or story.mentioned_in_story):
        # Still route if explicit story mention in text + media id.
        if not (_STORY_HINT_RE.search(incoming.text or "") and story.story_media_id):
            return False
    return story_has_explicit_product_intent(incoming.text)
