from __future__ import annotations

from app.configuration.runtime import message as operator_message

import html
import re

from app.channels.channel_profiles import get_channel_profile
from app.models import AgentResult, IncomingMessage
from app.llm.response_presenter import present_agent_result


_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def truncate_reply(text: str, max_chars: int) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars <= 0:
        return ""
    boundary = max_chars - 1
    # Treat URLs (including long signed checkout links) as indivisible tokens.
    for match in re.finditer(r"https?://[^\s<>]+", cleaned):
        if match.start() < boundary < match.end():
            # The configured length is a presentation preference. Preserve the
            # complete actionable link, including its preceding explanation.
            boundary = match.end()
            break
    cut = cleaned[:boundary].rstrip()
    # A link longer than the whole display budget cannot be shortened safely.
    # Preserve it when it is the only available content; channel transport has
    # its own hard limit, separate from the operator's preferred reply length.
    if not cut:
        match = re.match(r"https?://[^\s<>]+", cleaned)
        return match.group(0) if match else "…"[:max_chars]
    if boundary >= len(cleaned):
        return cleaned
    return cut + ("\n…" if re.search(r"https?://[^\s<>]+$", cut) else "…")


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
    # Ensure commercial products in metadata are authority-filtered before present.
    products = (result.commercial_data or {}).get("products")
    if isinstance(products, list) and products:
        from app.catalog.index.catalog_index import build_allowed_id_sets, filter_products_to_allowed
        from app.verify.fact_authority import authorize_products_for_responder

        tenant_id = str(
            (result.response_metadata or {}).get("tenant_id") or "newstore"
        )
        # Closed ID set from evidence already attached to the turn (if any).
        meta = dict(result.response_metadata or {})
        allowed_meta = meta.get("allowed_id_sets")
        if isinstance(allowed_meta, dict) and allowed_meta.get("allowed_product_ids"):
            allowed = {
                "allowed_product_ids": set(str(x) for x in allowed_meta["allowed_product_ids"]),
                "allowed_variant_ids": set(
                    str(x) for x in (allowed_meta.get("allowed_variant_ids") or [])
                ),
                "allowed_catalog_item_keys": set(
                    str(x)
                    for x in (allowed_meta.get("allowed_catalog_item_keys") or [])
                ),
            }
            products, invented = filter_products_to_allowed(
                [p for p in products if isinstance(p, dict)],
                allowed,
            )
            if invented:
                print(
                    "[composer.invented_product_rejected]",
                    {"count": invented, "tenant_id": tenant_id},
                )
        else:
            products = [p for p in products if isinstance(p, dict)]
            allowed = build_allowed_id_sets(products)
            meta["allowed_id_sets"] = {
                k: sorted(v) for k, v in allowed.items()
            }
        authorized, grounded = authorize_products_for_responder(
            products,
            tenant_id=tenant_id,
        )
        result.commercial_data = dict(result.commercial_data or {})
        result.commercial_data["products"] = authorized
        result.response_metadata = meta
        result.response_metadata.setdefault(
            "grounded_commerce_evidence",
            [row.model_dump(mode="json") for row in grounded[:40]],
        )
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
