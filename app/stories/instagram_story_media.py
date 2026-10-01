"""Safe Instagram Story media download and video frame extraction.

Operational URLs keep signed query strings. Logs only use SafeMediaReference.

The worker uses private temporary files and workspace-scoped Storage reads.
The bytes interface remains available to image and legacy callers.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import socket
import re
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import httpx

from app.config import get_settings
from app.stories.instagram_story_parser import safe_media_reference
from app.ops.observability import log_event


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

_DANGEROUS_HOST_GLOBS = ("*", "*.*", "0.0.0.0", "::")


@dataclass
class DownloadedStoryMedia:
    content: bytes
    content_type: str
    sha256: str
    final_host: str
    storage_path: str | None = None
    byte_count: int = 0


@dataclass
class StoryMediaFile:
    path: Path
    content_type: str
    sha256: str
    byte_count: int

    def close(self):
        self.path.unlink(missing_ok=True)


class StoryMediaError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class StoryMediaStorage(Protocol):
    async def put_private(
        self,
        *,
        content: bytes | Path,
        content_type: str,
        sha256: str,
        tenant_id: str,
    ) -> str | None: ...

    async def get_private(self, *, storage_path: str) -> bytes | None: ...

    async def delete(self, *, storage_path: str) -> bool: ...

    async def exists(self, *, storage_path: str) -> bool: ...


class SupabasePrivateStoryMediaStorage:
    """Private object storage under a non-public prefix. Never returns signed URLs."""

    def __init__(self, *, bucket: str | None = None):
        self.bucket = bucket
        self.last_error: str | None = None

    def _failed(self, code: str) -> None:
        self.last_error = code
        log_event("instagram_story.media_storage_failed", {"code": code})
        return None

    async def put_private(
        self,
        *,
        content: bytes | Path,
        content_type: str,
        sha256: str,
        tenant_id: str,
    ) -> str | None:
        settings = get_settings()
        self.last_error = None
        if not settings.supabase_url or not settings.supabase_service_key:
            return self._failed("storage_credentials_missing")
        bucket = str(
            self.bucket
            or getattr(settings, "instagram_story_storage_bucket", None)
            or getattr(settings, "supabase_story_media_bucket", None)
            or ""
        ).strip()
        if not bucket:
            # Do not reuse the public audio bucket as a silent fallback.
            return self._failed("storage_bucket_missing")
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', tenant_id) or not re.fullmatch(r'[a-f0-9]{64}', sha256):
            return self._failed("storage_identity_invalid")
        object_name = f"private/instagram-stories/{tenant_id}/{sha256[:48]}"
        upload_url = (
            f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
            f"{bucket}/{object_name}"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                auth_headers = {
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "apikey": settings.supabase_service_key,
                }
                # Refuse accidental archival into a public bucket.
                bucket_info = await client.get(
                    f"{settings.supabase_url.rstrip('/')}/storage/v1/bucket/{bucket}",
                    headers=auth_headers,
                )
                if bucket_info.status_code != 200:
                    return self._failed(f"storage_bucket_http_{bucket_info.status_code}")
                if bucket_info.json().get('public') is not False:
                    return self._failed("private_bucket_required")
                object_size = content.stat().st_size if isinstance(content, Path) else len(content)
                bucket_limit = bucket_info.json().get('file_size_limit')
                if bucket_limit and object_size > int(bucket_limit):
                    return self._failed('storage_object_too_large')
                response = await client.post(
                    upload_url,
                    content=_file_chunks(content) if isinstance(content, Path) else content,
                    headers={
                        **auth_headers,
                        "Content-Type": content_type,
                        "x-upsert": "true",
                        "Content-Length": str(content.stat().st_size if isinstance(content, Path) else len(content)),
                    },
                )
                if response.status_code >= 400:
                    return self._failed(f"storage_upload_http_{response.status_code}")
            return f"supabase://{bucket}/{object_name}"
        except Exception as exc:  # noqa: BLE001
            return self._failed(type(exc).__name__)

    async def get_private(self, *, storage_path: str) -> bytes | None:
        return None  # reserved — callers should not need public fetch

    async def delete(self, *, storage_path: str) -> bool:
        settings = get_settings()
        if not storage_path.startswith("supabase://"):
            return False
        if not settings.supabase_url or not settings.supabase_service_key:
            return False
        try:
            _, rest = storage_path.split("supabase://", 1)
            bucket, object_name = rest.split("/", 1)
        except ValueError:
            return False
        delete_url = (
            f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
            f"{bucket}/{object_name}"
        )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.delete(
                    delete_url,
                    headers={"Authorization": f"Bearer {settings.supabase_service_key}"},
                )
            return response.status_code < 400
        except Exception:
            return False

    async def exists(self, *, storage_path: str) -> bool:
        return bool(storage_path)


def _configured_allowed_suffixes() -> list[str]:
    settings = get_settings()
    extras: list[str] = []
    for part in (getattr(settings, "instagram_story_allowed_hosts", "") or "").split(","):
        host = part.strip().casefold().lstrip(".")
        if not host:
            continue
        if host in _DANGEROUS_HOST_GLOBS or "*" in host or host.startswith("."):
            raise StoryMediaError("allowed_hosts_invalid")
        if "/" in host or " " in host:
            raise StoryMediaError("allowed_hosts_invalid")
        extras.append(host)
    return extras + list(_DEFAULT_ALLOWED_SUFFIXES)


def _allowed_host(host: str) -> bool:
    host_l = host.casefold().rstrip(".")
    for suffix in _configured_allowed_suffixes():
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


def validate_story_media_url(url: str) -> tuple[str, list[str]]:
    """Validate operational URL. Returns (url, resolved_ip_strings).

    Uses the shared HTTPS/allowlist/DNS policy in ``app.core.remote_media``.
    Preserves the full signed URL. DNS rebinding residual risk is documented:
    we resolve once before connect; httpx may re-resolve — prefer egress allowlist
    in production.
    """
    from app.core.remote_media import (
        RemoteMediaError,
        validate_remote_media_url_with_ips,
    )

    suffixes = tuple(_configured_allowed_suffixes())
    try:
        return validate_remote_media_url_with_ips(
            url,
            allowed_suffixes=suffixes,
            resolver=socket.getaddrinfo,
        )
    except RemoteMediaError as exc:
        raise StoryMediaError(exc.code) from exc


def _sniff_mime(content: bytes) -> str | None:
    if not content:
        return None
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
        return "image/webp"
    if b"ftyp" in content[:64]:
        return "video/mp4"
    if content.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html", b"<svg")):
        return "text/html"
    if content[:2] in {b"MZ", b"\x7fE"}:
        return "application/octet-stream"
    return None


async def _stream_once(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
) -> tuple[int, bytes, str, str]:
    async with client.stream(
        "GET",
        url,
        follow_redirects=False,
        headers={"User-Agent": "NSAgentStoryMedia/2.0"},
    ) as response:
        status = response.status_code
        if status in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            return status, b"", "", location or ""
        if status >= 400:
            raise StoryMediaError(f"http_{status}")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise StoryMediaError("file_too_large")
            except ValueError:
                pass
        header_ct = str(response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if header_ct and not (
            header_ct.startswith("image/")
            or header_ct.startswith("video/")
            or header_ct in {"application/octet-stream"}
        ):
            raise StoryMediaError("mime_invalid")
        total = 0
        chunks: list[bytes] = []
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise StoryMediaError("file_too_large")
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content:
            raise StoryMediaError("empty_body")
        return status, content, header_ct, ""


async def download_story_media(
    url: str,
    *,
    tenant_id: str = "unknown",
    storage: StoryMediaStorage | None = None,
    persist: bool = True,
    max_bytes: int | None = None,
) -> DownloadedStoryMedia:
    settings = get_settings()
    max_bytes = max_bytes or int(getattr(settings, "instagram_story_media_max_bytes", 104_857_600) or 104_857_600)
    timeout = float(getattr(settings, "instagram_story_media_timeout_seconds", 10) or 10)
    max_redirects = 3

    current, _resolved = validate_story_media_url(url)
    seen: set[str] = set()
    log_ref = safe_media_reference(current)
    log_event(
        "instagram_story.media_download_started",
        {
            "host": log_ref.host if log_ref else None,
            "path_hash": log_ref.path_hash if log_ref else None,
            "max_bytes": max_bytes,
        },
    )

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            redirects = 0
            while True:
                if current in seen:
                    raise StoryMediaError("redirect_loop")
                seen.add(current)
                status, content, header_ct, location = await _stream_once(
                    client, current, max_bytes=max_bytes
                )
                if status in {301, 302, 303, 307, 308}:
                    redirects += 1
                    if redirects > max_redirects:
                        raise StoryMediaError("redirect_limit_exceeded")
                    if not location:
                        raise StoryMediaError("redirect_missing_location")
                    next_url = urljoin(current, location)
                    try:
                        current, _ = validate_story_media_url(next_url)
                    except StoryMediaError as exc:
                        if exc.code == "host_not_allowed":
                            raise StoryMediaError("redirect_host_not_allowed") from exc
                        if exc.code == "private_ip_blocked":
                            raise StoryMediaError("redirect_private_ip") from exc
                        raise
                    continue

                sniffed = _sniff_mime(content)
                if sniffed == "text/html":
                    raise StoryMediaError("html_disguised")
                if sniffed is None and not (
                    header_ct.startswith("image/") or header_ct.startswith("video/")
                ):
                    raise StoryMediaError("mime_invalid")
                content_type = sniffed or header_ct or "application/octet-stream"
                if not (
                    content_type.startswith("image/") or content_type.startswith("video/")
                ):
                    raise StoryMediaError("mime_invalid")
                if header_ct.startswith("image/") and sniffed and not sniffed.startswith("image/"):
                    raise StoryMediaError("mime_mismatch")
                if header_ct.startswith("video/") and sniffed and not sniffed.startswith("video/"):
                    raise StoryMediaError("mime_mismatch")

                digest = hashlib.sha256(content).hexdigest()
                storage_path = None
                if persist and bool(getattr(settings, "instagram_story_media_storage_enabled", True)):
                    backend = storage or SupabasePrivateStoryMediaStorage()
                    storage_path = await backend.put_private(
                        content=content,
                        content_type=content_type,
                        sha256=digest,
                        tenant_id=tenant_id,
                    )
                    # Never invent local:// paths when nothing was stored.
                return DownloadedStoryMedia(
                    content=content,
                    content_type=content_type,
                    sha256=digest,
                    final_host=urlparse(current).hostname or "unknown",
                    storage_path=storage_path,
                    byte_count=len(content),
                )
    except StoryMediaError as exc:
        log_event(
            "instagram_story.media_download_failed",
            {
                "code": exc.code,
                "host": log_ref.host if log_ref else None,
                "path_hash": log_ref.path_hash if log_ref else None,
            },
        )
        raise
    except Exception as exc:  # noqa: BLE001
        log_event(
            "instagram_story.media_download_failed",
            {
                "code": type(exc).__name__,
                "host": log_ref.host if log_ref else None,
            },
        )
        raise StoryMediaError("download_failed") from exc


def extract_video_frames_best_effort(
    content: bytes | Path,
    *,
    max_frames: int = 8,
    frame_times: list[float] | None = None,
) -> list[bytes]:
    """Decode sharp representative JPEG frames from bytes or a private file."""
    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_video_frame_analysis_enabled", False)):
        return []
    if not content:
        return []

    frame_limit = max(1, min(int(max_frames or 8), 10))
    try:
        from decord import VideoReader, cpu
        from PIL import Image

        reader = VideoReader(str(content) if isinstance(content, Path) else io.BytesIO(content), ctx=cpu(0), num_threads=1)
        frame_count = len(reader)
        if frame_count <= 0:
            return []
        sample_limit = min(40, frame_limit * 4)
        frame_indexes = sorted(
            {
                min(
                    frame_count - 1,
                    # Cover opening, closing and intermediate product shots.
                    max(0, round((frame_count - 1) * (0.02 + 0.96 * index / (sample_limit - 1))))
                    if sample_limit > 1 else frame_count // 2,
                )
                for index in range(sample_limit)
            }
        )

        import numpy as np
        selected = []
        # Retain only the best frame per time window, not all 40 decoded images.
        # Fetch individually to avoid native get_batch hangs on malformed containers.
        for index in range(frame_limit):
            start = len(frame_indexes) * index // frame_limit
            stop = len(frame_indexes) * (index + 1) // frame_limit
            best = None
            for frame_index in frame_indexes[start:stop]:
                image = Image.fromarray(reader[frame_index].asnumpy()).convert("RGB")
                image.thumbnail((1600, 1600))
                gray = np.asarray(image.convert("L").resize((128, 128)), dtype=float)
                sharpness = float(np.var(np.diff(gray, axis=0)) + np.var(np.diff(gray, axis=1)))
                if not 4 < float(gray.mean()) < 251:
                    sharpness = -1
                if best is None or sharpness > best[1]:
                    best = (frame_index, sharpness, image)
            if best is not None:
                selected.append(best)
        encoded: list[bytes] = []
        seen: set[str] = set()
        thumbnails = []
        for frame_index, sharpness, image in selected:
            small = np.asarray(image.resize((32, 32)), dtype=float)
            if any(float(np.mean(np.abs(small - previous))) < 2 for previous in thumbnails):
                continue
            thumbnails.append(small)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
            payload = output.getvalue()
            digest = hashlib.sha256(payload).hexdigest()
            if digest not in seen:
                seen.add(digest)
                encoded.append(payload)
                if frame_times is not None:
                    frame_times.append(round(frame_index / float(reader.get_avg_fps()), 3))
        log_event(
            "instagram_story.video_frames_extracted",
            {"frames": len(encoded), "requested": frame_limit, "sampled": len(frame_indexes),
             "selection": "temporal_sharpness", "frame_indexes": [item[0] for item in selected]},
        )
        return encoded
    except Exception as exc:  # noqa: BLE001 - media decoding must degrade safely
        log_event(
            "instagram_story.video_frame_extraction_failed",
            {"code": type(exc).__name__},
        )
        return []


async def _file_chunks(path: Path):
    with path.open("rb") as stream:
        while chunk := stream.read(256 * 1024):
            yield chunk


async def download_story_media_file(url: str, *, max_bytes: int | None = None,
                                    private_path: str | None = None,
                                    workspace_id: str | None = None) -> StoryMediaFile:
    """Stream large media to a private temporary file, deleting partial downloads.

    Only server-owned Storage paths scoped to the workspace may use credentials.
    Remote redirects never receive Storage credentials.
    """
    settings = get_settings()
    limit = max_bytes or settings.instagram_story_media_max_bytes
    headers = {"User-Agent": "NSAgentStoryWorker/1.0"}
    if private_path:
        prefix = f"supabase://conversation-media/private/instagram-stories/{workspace_id}/"
        if not workspace_id or not private_path.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{48}", private_path[len(prefix):]):
            raise StoryMediaError("storage_scope_invalid")
        if not settings.supabase_url or not settings.supabase_service_key:
            raise StoryMediaError("storage_credentials_missing")
        url = settings.supabase_url.rstrip('/') + '/storage/v1/object/authenticated/' + private_path[len('supabase://'):]
        headers['Authorization'] = f"Bearer {settings.supabase_service_key}"
    else:
        url, _ = validate_story_media_url(url)
    handle = tempfile.NamedTemporaryFile(prefix="story-", suffix=".media", delete=False)
    path = Path(handle.name)
    handle.close()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
            for redirect in range(4):
                async with client.stream("GET", url, headers=headers) as response:
                    if response.is_redirect:
                        if private_path or redirect == 3 or not response.headers.get('location'):
                            raise StoryMediaError("redirect_limit_exceeded")
                        url, _ = validate_story_media_url(urljoin(url, response.headers['location']))
                        continue
                    if response.status_code >= 400:
                        raise StoryMediaError(f"http_{response.status_code}")
                    try:
                        if int(response.headers.get('content-length', 0)) > limit:
                            raise StoryMediaError('file_too_large')
                    except ValueError:
                        pass
                    count, digest, head = 0, hashlib.sha256(), b''
                    with path.open('wb') as output:
                        async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                            count += len(chunk)
                            if count > limit:
                                raise StoryMediaError('file_too_large')
                            if len(head) < 4096:
                                head = (head + chunk)[:4096]
                            digest.update(chunk)
                            output.write(chunk)
                    if not count:
                        raise StoryMediaError('empty_body')
                    mime = _sniff_mime(head)
                    header = response.headers.get('content-type', '').split(';')[0].lower().strip()
                    if mime == 'text/html':
                        raise StoryMediaError('html_disguised')
                    if not mime or not mime.startswith(('image/', 'video/')):
                        raise StoryMediaError('mime_invalid')
                    if header.startswith(('image/', 'video/')) and header.split('/')[0] != mime.split('/')[0]:
                        raise StoryMediaError('mime_mismatch')
                    return StoryMediaFile(path, mime, digest.hexdigest(), count)
        raise StoryMediaError('redirect_limit_exceeded')
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def safe_media_url_for_log(url: str | None) -> dict[str, Any]:
    ref = safe_media_reference(url)
    if ref is None:
        return {"present": False}
    return ref.model_dump(mode="json")
