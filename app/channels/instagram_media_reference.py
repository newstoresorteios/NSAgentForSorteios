"""Distinguish Instagram pages from downloadable media, without fetching pages."""

import re
from urllib.parse import urlsplit

from app.models import IncomingMessage


def is_instagram_publication_url(value: str | None) -> bool:
    try:
        url = urlsplit(str(value or "").strip())
        return (
            url.scheme in {"https", "http"}
            and (url.hostname or "").lower() in {"instagram.com", "www.instagram.com", "m.instagram.com"}
            and url.path.strip("/").split("/", 1)[0] in {"p", "reel", "reels", "stories", "share", "tv"}
        )
    except ValueError:
        return False


def has_unresolved_instagram_publication(message: IncomingMessage) -> bool:
    if is_instagram_publication_url(message.image_url):
        return True
    if message.image_url or message.instagram_story is not None:
        return False
    return bool((message.channel_metadata or {}).get("instagram_media_unresolved")) or any(
        is_instagram_publication_url(url)
        for url in re.findall(r"https?://[^\s<>]+", message.text or "", re.I)
    )
