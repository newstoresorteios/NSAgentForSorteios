"""Safe Instagram Story media download (SSRF-hardened).

Never logs signed URLs or tokens. Stores only internal paths when enabled.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import get_settings
from .instagram_story_parser import strip_signed_url
from .observability import log_event


# Default CDN / Meta / Brevo-related hosts when INSTAGRAM_STORY_ALLOWED_HOSTS is empty.
_DEFAULT_ALLOWED_SUFFIXES = (
    "fbcdn.net",
    "cdninstagram.com",
    "instagram.com",
    "facebook.com",
    "fbsbx.com",
    "brevo.com",
    "sendinblue.com",
    "sibpages.com",
)


@dataclass
class DownloadedStoryMedia:
    content: bytes
    content_type: str
    sha256: str
    final_host: str
    storage_path: str | None = None


class StoryMediaError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _allowed_host(host: str, configured: str) -> bool:
    host_l = host.casefold().rstrip(".")
    extras = [
        part.strip().casefold()
        for part in (configured or "").split(",")
        if part.strip()
    ]
    suffixes = extras + list(_DEFAULT_ALLOWED_SUFFIXES)
    for suffix in suffixes:
        if host_l == suffix or host_l.endswith("." + suffix):
            return True
    return False


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or str(ip) in {"169.254.169.254", "metadata.google.internal"}
    )


def validate_story_media_url(url: str) -> str:
    """Validate URL before download. Returns cleaned URL (query stripped for logging only)."""
    settings = get_settings()
    text = (url or "").strip()
    if not text:
        raise StoryMediaError("url_missing")
    parsed = urlparse(text)
    if parsed.scheme != "https":
        raise StoryMediaError("scheme_not_https")
    host = parsed.hostname or ""
    if not host:
        raise StoryMediaError("host_missing")
    if host.casefold() in {"localhost", "metadata", "metadata.google.internal"}:
        raise StoryMediaError("host_blocked")
    if not _allowed_host(host, getattr(settings, "instagram_story_allowed_hosts", "") or ""):
        raise StoryMediaError("host_not_allowed")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise StoryMediaError("dns_failed") from exc
    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise StoryMediaError("private_ip_blocked")
    return text


def _sniff_mime(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if content[:12] == b"\x00\x00\x00\x18ftypmp4" or b"ftyp" in content[:64]:
        return "video/mp4"
    if content.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        return "text/html"
    return None


async def download_story_media(url: str) -> DownloadedStoryMedia:
    settings = get_settings()
    validate_story_media_url(url)
    max_bytes = int(getattr(settings, "instagram_story_media_max_bytes", 12_582_912) or 12_582_912)
    timeout = float(getattr(settings, "instagram_story_media_timeout_seconds", 10) or 10)
    host_for_log = urlparse(url).hostname or "unknown"
    log_event(
        "instagram_story.media_download_started",
        {"host": host_for_log, "max_bytes": max_bytes},
    )
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=3,
        ) as client:
            # Pre-validate each redirect target host.
            response = await client.get(url, headers={"User-Agent": "NSAgentStoryMedia/1.0"})
            # Validate final URL host as well.
            validate_story_media_url(str(response.url))
            if response.status_code >= 400:
                raise StoryMediaError(f"http_{response.status_code}")
            content = response.content
            if len(content) > max_bytes:
                raise StoryMediaError("file_too_large")
            sniffed = _sniff_mime(content)
            if sniffed == "text/html":
                raise StoryMediaError("html_disguised")
            header_ct = str(response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if sniffed is None and not (
                header_ct.startswith("image/") or header_ct.startswith("video/")
            ):
                raise StoryMediaError("mime_invalid")
            content_type = sniffed or header_ct or "application/octet-stream"
            if not (
                content_type.startswith("image/") or content_type.startswith("video/")
            ):
                raise StoryMediaError("mime_invalid")
            digest = hashlib.sha256(content).hexdigest()
            storage_path = None
            if bool(getattr(settings, "instagram_story_media_storage_enabled", True)):
                storage_path = await _store_private_best_effort(
                    content,
                    content_type=content_type,
                    sha256=digest,
                )
            return DownloadedStoryMedia(
                content=content,
                content_type=content_type,
                sha256=digest,
                final_host=urlparse(str(response.url)).hostname or host_for_log,
                storage_path=storage_path,
            )
    except StoryMediaError as exc:
        log_event(
            "instagram_story.media_download_failed",
            {"code": exc.code, "host": host_for_log},
        )
        raise
    except Exception as exc:  # noqa: BLE001
        log_event(
            "instagram_story.media_download_failed",
            {"code": type(exc).__name__, "host": host_for_log},
        )
        raise StoryMediaError("download_failed") from exc


async def _store_private_best_effort(
    content: bytes,
    *,
    content_type: str,
    sha256: str,
) -> str | None:
    """Best-effort private object path. Does not return signed URLs."""
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        return f"local://story-media/{sha256[:32]}"
    bucket = getattr(settings, "supabase_audio_bucket", None) or "agent-audio"
    # Keep story media under a private prefix; callers must not expose public URLs.
    object_name = f"instagram-stories/{sha256[:40]}"
    upload_url = (
        f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
        f"{bucket}/{object_name}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                upload_url,
                content=content,
                headers={
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "Content-Type": content_type,
                    "x-upsert": "true",
                },
            )
            if response.status_code >= 400:
                return f"local://story-media/{sha256[:32]}"
        return f"supabase://{bucket}/{object_name}"
    except Exception:
        return f"local://story-media/{sha256[:32]}"


def extract_video_frames_best_effort(
    content: bytes,
    *,
    max_frames: int = 3,
) -> list[bytes]:
    """Optional video frame extraction.

    Without a video decoder in the deploy image, returns empty and callers must
    fall back to thumbnail URL or ask the customer for a print.
    """
    _ = content
    _ = max_frames
    # Documented dependency (not required for image Stories):
    #   opencv-python-headless OR imageio+ffmpeg — confirm Render/Vercel size limits
    #   before adding. Interface reserved for future wiring.
    return []


def safe_media_url_for_log(url: str | None) -> dict[str, Any]:
    cleaned = strip_signed_url(url)
    if not cleaned:
        return {"present": False}
    parsed = urlparse(cleaned)
    return {"present": True, "host": parsed.hostname, "path_hash": hashlib.sha256(parsed.path.encode()).hexdigest()[:12]}
