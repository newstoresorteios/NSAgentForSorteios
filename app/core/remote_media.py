"""Shared HTTPS media fetch (Stories-quality SSRF policy)."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

from app.config import get_settings

DEFAULT_ALLOWED_SUFFIXES = (
    "fbcdn.net",
    "cdninstagram.com",
    "instagram.com",
    "facebook.com",
    "fbsbx.com",
    "whatsapp.net",
    "whatsapp.com",
    "brevo.com",
    "sendinblue.com",
    "sibpages.com",
)


class RemoteMediaError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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


def _allowed_host(host: str, suffixes: tuple[str, ...]) -> bool:
    host_l = host.casefold().rstrip(".")
    for suffix in suffixes:
        if host_l == suffix or host_l.endswith("." + suffix):
            return True
    return False


def resolve_public_host_ips(host: str, *, resolver=None) -> list[str]:
    lookup = resolver or socket.getaddrinfo
    try:
        infos = lookup(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RemoteMediaError("dns_failed") from exc
    resolved: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise RemoteMediaError("private_ip_blocked")
        resolved.append(str(ip))
    if not resolved:
        raise RemoteMediaError("dns_failed")
    return resolved


def validate_remote_media_url(
    url: str,
    *,
    allowed_suffixes: tuple[str, ...] = DEFAULT_ALLOWED_SUFFIXES,
    resolver=None,
) -> str:
    text, _ips = validate_remote_media_url_with_ips(
        url,
        allowed_suffixes=allowed_suffixes,
        resolver=resolver,
    )
    return text


def validate_remote_media_url_with_ips(
    url: str,
    *,
    allowed_suffixes: tuple[str, ...] = DEFAULT_ALLOWED_SUFFIXES,
    resolver=None,
) -> tuple[str, list[str]]:
    text = (url or "").strip()
    if not text:
        raise RemoteMediaError("url_missing")
    parsed = urlparse(text)
    if parsed.scheme != "https":
        raise RemoteMediaError("scheme_not_https")
    host = parsed.hostname or ""
    if not host:
        raise RemoteMediaError("host_missing")
    if host.casefold() in {"localhost", "metadata", "metadata.google.internal"}:
        raise RemoteMediaError("host_blocked")
    if not _allowed_host(host, allowed_suffixes):
        raise RemoteMediaError("host_not_allowed")
    return text, resolve_public_host_ips(host, resolver=resolver)


async def download_trusted_media(
    url: str,
    *,
    kind: str,
    max_bytes: int,
    timeout_seconds: float = 20,
    allowed_suffixes: tuple[str, ...] | None = None,
) -> tuple[bytes, str]:
    suffixes = allowed_suffixes or DEFAULT_ALLOWED_SUFFIXES
    extras = str(getattr(get_settings(), "instagram_story_allowed_hosts", "") or "")
    extra_suffixes = tuple(
        part.strip().casefold().lstrip(".")
        for part in extras.split(",")
        if part.strip()
    )
    allowed = suffixes + extra_suffixes
    current = validate_remote_media_url(url, allowed_suffixes=allowed)
    seen: set[str] = set()
    prefix = "audio/" if kind == "audio" else "image/"
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
        for _ in range(4):
            if current in seen:
                raise RemoteMediaError("redirect_loop")
            seen.add(current)
            async with client.stream(
                "GET",
                current,
                follow_redirects=False,
                headers={"User-Agent": "NSAgentRemoteMedia/1.0"},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise RemoteMediaError("redirect_missing_location")
                    current = validate_remote_media_url(
                        urljoin(current, location),
                        allowed_suffixes=allowed,
                    )
                    continue
                if response.status_code >= 400:
                    raise RemoteMediaError(f"http_{response.status_code}")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        if int(content_length) > max_bytes:
                            raise RemoteMediaError("file_too_large")
                    except ValueError:
                        pass
                header_ct = str(response.headers.get("content-type") or "").split(";")[0].strip()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise RemoteMediaError("file_too_large")
                    chunks.append(chunk)
                content = b"".join(chunks)
                if not content:
                    raise RemoteMediaError("empty_body")
                if header_ct.startswith(prefix):
                    return content, header_ct
                if kind == "audio" and header_ct in {"", "application/octet-stream"}:
                    return content, header_ct or "audio/ogg"
                raise RemoteMediaError("mime_invalid")
        raise RemoteMediaError("redirect_limit_exceeded")
