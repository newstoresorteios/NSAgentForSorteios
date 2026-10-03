"""Attach catalog photos only for products actually present in the final reply."""
from urllib.parse import urlsplit

from app.models import AgentResult, IncomingMessage


def safe_catalog_photo(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and (host == "tcdn.com.br" or host.endswith(".tcdn.com.br"))):
            return value
    except ValueError:
        pass
    return None


def attach_presented_product_photos(incoming: IncomingMessage, result: AgentResult) -> AgentResult:
    if incoming.provider != "meta" or incoming.channel != "instagram" or result.handoff_required:
        return result
    from app.catalog.media.product_media import official_product_image
    photos = []
    text = result.reply_text or ""
    for product in (result.commercial_data or {}).get("products") or []:
        if not isinstance(product, dict):
            continue
        # No stale active product, search candidate, or product cut by the presenter.
        name, url = str(product.get("name") or ""), str(product.get("url") or "")
        if not ((name and name in text) or (url and url in text)):
            continue
        photo = safe_catalog_photo(official_product_image(product))
        if photo and photo not in photos:
            photos.append(photo)
        if len(photos) == 10:
            break
    if photos:
        result.response_metadata["outbound_image_urls"] = photos
        result.response_metadata["outbound_image_url"] = photos[0]
    return result


def outbound_catalog_photos(result: AgentResult) -> list[str]:
    metadata = result.response_metadata
    values = metadata.get("outbound_image_urls") or [metadata.get("outbound_image_url")]
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(url for value in values if (url := safe_catalog_photo(value))))[:10]
