from __future__ import annotations

import html
import re

from .channel_profiles import get_channel_profile
from .models import AgentResult, IncomingMessage
from .response_presenter import present_agent_result


_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def truncate_reply(text: str, max_chars: int) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    # Prefer not to cut mid-URL when possible.
    cut = cleaned[: max_chars - 1].rstrip()
    if "http" in cut and "://" in cleaned[max_chars - 40 : max_chars + 40]:
        last_space = cut.rfind(" ")
        if last_space > max_chars // 2:
            cut = cut[:last_space].rstrip()
    return cut + "…"


def normalize_reply_text(text: str) -> str:
    value = html.unescape((text or "").replace("\r\n", "\n").strip())
    value = _MULTI_SPACE_RE.sub(" ", value)
    value = _MULTI_BLANK_RE.sub("\n\n", value)
    return value.strip()


def compose_outbound_reply(
    incoming: IncomingMessage,
    result: AgentResult,
    *,
    max_reply_chars: int | None = None,
) -> AgentResult:
    profile = get_channel_profile(incoming.channel)
    limit = max_reply_chars or profile.max_reply_chars
    result = present_agent_result(incoming, result)
    result.reply_text = truncate_reply(
        normalize_reply_text(result.reply_text),
        limit,
    )
    if not profile.allow_audio_reply and result.reply_modality == "audio":
        result.reply_modality = "text"
        result.reply_audio_bytes = None
        result.reply_audio_mime_type = None
        result.reply_audio_url = None
    result.response_metadata["channel_profile"] = {
        "channel": profile.channel,
        "tone": profile.tone,
        "assisted_chat": profile.assisted_chat,
        "max_reply_chars": limit,
        "allow_audio_reply": profile.allow_audio_reply,
        "presentation_style": profile.tone,
    }
    return result
