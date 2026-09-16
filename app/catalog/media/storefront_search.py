"""Search the New Store vitrine when Tray name search misses a listing."""

from __future__ import annotations

from io import BytesIO
import httpx
import json
import re
from typing import Any
from urllib.parse import quote_plus
from app.configuration.runtime import policy
from app.catalog.media.storefront_evidence import json_policy

from app.ops.observability import log_event


class StorefrontSearchResults(list):
    """List-compatible search result that records whether pagination ended."""

    def __init__(self, values=(), *, complete: bool = True):
        super().__init__(values)
        self.complete = complete

_ITEM_RE = re.compile(
    r'"item_id"\s*:\s*"(\d+)"\s*,\s*"item_name"\s*:\s*"((?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)
_HREF_RE = re.compile(
    r'href="(https://www\.newstorerj\.com(?:\.br)?/(?:relogios[^/]*/relogio-|relogio-seminovo-)[^"]+)"',
    re.IGNORECASE,
)
_REF_RE = re.compile(r"(c\d{2}-\d{2}[a-z0-9-]+)", re.IGNORECASE)
_LIST_PRODUCTS_RE = re.compile(
    r'"listProducts"\s*:\s*(\[.*?\])\s*,\s*"filter"\s*:',
    re.IGNORECASE | re.DOTALL,
)


def _product_from_storefront_hit(hit: dict[str, str]) -> dict[str, Any]:
    """Minimal catalog row when Tray get_product is unavailable."""
    product_id = str(hit.get("product_id") or "").strip()
    name = str(hit.get("name") or "").strip()
    url = str(hit.get("url") or "").strip()
    reference = str(hit.get("reference") or "").strip()
    brand = str(hit.get("brand") or "").strip()
    return {
        "id": product_id,
        "name": name,
        "title": name,
        "url": url or None,
        "image_url": str(hit.get("image_url") or "").strip() or None,
        "reference": reference or None,
        "model": str(hit.get("model") or "").strip() or None,
        "brand": brand or None,
        "price": hit.get("price"),
        "current_price": hit.get("price"),
        "storefront_only": True,
    }


def _decode_js_string(raw: str) -> str:
    text = raw.replace(r"\/", "/")
    try:
        return bytes(text, "utf-8").decode("unicode_escape")
    except Exception:
        return text


def parse_storefront_search_html(html: str) -> list[dict[str, str]]:
    rich = _LIST_PRODUCTS_RE.search(html or "")
    if rich:
        try:
            rows = json.loads(rich.group(1))
        except (json.JSONDecodeError, TypeError):
            rows = []
        hits = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            product_id = str(row.get("idProduct") or "").strip()
            name = str(row.get("nameProduct") or "").strip()
            if not product_id or not name:
                continue
            raw_url = str(row.get("urlProduct") or "").replace(r"\/", "/").strip()
            if raw_url.startswith("http://"):
                raw_url = "https://" + raw_url[7:]
            hits.append({
                "product_id": product_id,
                "name": name,
                "reference": str(row.get("reference") or "").strip(),
                "brand": str(row.get("brand") or "").strip(),
                "model": str(row.get("model") or "").strip(),
                "url": raw_url,
                "image_url": str(row.get("urlImage") or "").replace(r"\/", "/").strip(),
                "price": str(row.get("sellPrice") or row.get("price") or "").strip(),
            })
        if hits:
            return hits
    hits: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _ITEM_RE.finditer(html or ""):
        product_id = match.group(1)
        if product_id in seen:
            continue
        seen.add(product_id)
        name = _decode_js_string(match.group(2))
        ref_match = _REF_RE.search(name.replace(" ", "-"))
        hits.append(
            {
                "product_id": product_id,
                "name": name,
                "reference": ref_match.group(1).upper() if ref_match else "",
            }
        )
    if hits:
        return hits
    for href in _HREF_RE.findall(html or ""):
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        if slug in seen:
            continue
        seen.add(slug)
        ref_match = _REF_RE.search(slug)
        hits.append(
            {
                "product_id": "",
                "name": slug.replace("-", " "),
                "reference": ref_match.group(1).upper() if ref_match else "",
                "url": href,
            }
        )
    return hits


def perceptual_image_hash(image_bytes: bytes) -> int:
    """Compact difference hash; stable across WhatsApp resizing/recompression."""
    from PIL import Image

    image = Image.open(BytesIO(image_bytes)).convert("L").resize((17, 16))
    pixels = list(image.get_flattened_data())
    value = 0
    for y in range(16):
        for x in range(16):
            if pixels[y * 17 + x] > pixels[y * 17 + x + 1]:
                value |= 1 << (y * 16 + x)
    return value


def _center_crop_to_aspect(image, aspect: float):
    width, height = image.size
    current = width / height
    if abs(current - aspect) < 0.01:
        return image
    if current > aspect:
        crop_width = max(2, round(height * aspect))
        left = (width - crop_width) // 2
        return image.crop((left, 0, left + crop_width, height))
    crop_height = max(2, round(width / aspect))
    top = (height - crop_height) // 2
    return image.crop((0, top, width, top + crop_height))


def _comparison_views(image_bytes: bytes, *, extra_aspects=()):
    """Generate centered views so detail photos can match a full catalog image."""
    from PIL import Image

    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    bases = [image]
    for aspect in extra_aspects:
        adjusted = _center_crop_to_aspect(image, aspect)
        if adjusted.size != image.size:
            bases.append(adjusted)
    views = []
    for base in bases:
        width, height = base.size
        views.append(base)
        for ratio in (0.85, 0.70, 0.55, 0.40):
            crop_width = max(2, round(width * ratio))
            crop_height = max(2, round(height * ratio))
            left = (width - crop_width) // 2
            top = (height - crop_height) // 2
            views.append(base.crop((left, top, left + crop_width, top + crop_height)))
    return views


def _view_metrics(left, right) -> tuple[int, float]:
    from PIL import ImageChops, ImageStat

    left_small = left.resize((32, 32))
    right_small = right.resize((32, 32))
    left_stream = BytesIO()
    right_stream = BytesIO()
    left_small.save(left_stream, format="PNG")
    right_small.save(right_stream, format="PNG")
    distance = (
        perceptual_image_hash(left_stream.getvalue())
        ^ perceptual_image_hash(right_stream.getvalue())
    ).bit_count()
    error = sum(
        ImageStat.Stat(ImageChops.difference(left_small, right_small)).mean
    ) / (3 * 255)
    return distance, error


def best_image_view_metrics(left_bytes: bytes, right_bytes: bytes) -> tuple[int, float, int, int]:
    """Return the best full-frame or centered-crop comparison."""
    from PIL import Image

    left_image = Image.open(BytesIO(left_bytes))
    right_image = Image.open(BytesIO(right_bytes))
    left_aspect = left_image.width / left_image.height
    right_aspect = right_image.width / right_image.height
    rows = (
        (distance, error, left_index, right_index)
        for left_index, left_view in enumerate(
            _comparison_views(left_bytes, extra_aspects=(right_aspect,))
        )
        for right_index, right_view in enumerate(
            _comparison_views(right_bytes, extra_aspects=(left_aspect,))
        )
        for distance, error in [_view_metrics(left_view, right_view)]
    )
    return min(rows, key=lambda row: (row[0], row[1]))


async def rank_storefront_hits_by_image(
    image_url: str,
    hits: list[dict[str, str]],
    *,
    limit: int = 8,
) -> list[tuple[int, dict[str, str]]]:
    """Rank official storefront candidates against the inbound customer image."""
    import asyncio
    from app.catalog.vision.identify import download_image_file
    from app.core.remote_media import download_trusted_media
    from app.config import get_settings

    candidates = [hit for hit in hits[: max(1, limit)] if hit.get("image_url")]
    if not image_url or not candidates:
        return []
    source, _ = await download_image_file(image_url)
    semaphore = asyncio.Semaphore(max(1, min(8, int(policy('imageStorefrontDownloadConcurrency')))))

    async def _distance(hit: dict[str, str]):
        try:
            async with semaphore:
                content, _ = await download_trusted_media(
                    str(hit['image_url']), kind='image',
                    max_bytes=int(get_settings().agent_image_download_max_bytes),
                    timeout_seconds=10,
                    allowed_suffixes=tuple(json_policy('storefrontImageHosts')),
                )
            distance, error, source_view, candidate_view = best_image_view_metrics(
                source, content
            )
            return distance, {
                **hit,
                '_image_color_error': error,
                '_image_source_view': source_view,
                '_image_candidate_view': candidate_view,
            }
        except (OSError, ValueError, httpx.HTTPError):
            return None

    rows = await asyncio.gather(*[_distance(hit) for hit in candidates])
    ranked = [row for row in rows if row is not None]
    ranked.sort(key=lambda row: row[0])
    return ranked


async def storefront_product_available(url: str) -> bool | None:
    """Read the official product page availability without treating it as purchasable."""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0),
            follow_redirects=True,
            headers={"User-Agent": "NSAgentForSorteios/storefront-product-status"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError:
        return None
    from app.catalog.media.storefront_evidence import product_page_evidence
    evidence = product_page_evidence(response.text)
    return evidence['available'] if evidence else None


async def search_storefront(query: str, *, max_pages: int = 1, limit: int = 8) -> list[dict[str, str]]:
    """Return product ids/names from the official storefront search."""
    q = str(query or "").strip()
    if len(q) < 3:
        return []
    import httpx

    timeout = httpx.Timeout(8.0, connect=3.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "NSAgentForSorteios/storefront-search"},
    ) as client:
        for host in json_policy('storefrontHosts'):
            url = (
                f"https://{host}/loja/busca.php?loja={policy('storefrontStoreCode')}"
                f"&palavra_busca={quote_plus(q)}"
            )
            try:
                response = await client.get(url)
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            hits = parse_storefront_search_html(response.text)
            if hits:
                log_event(
                    "story_storefront_search",
                    {"host": host, "query": q[:40], "hits": len(hits)},
                )
                seen = {hit.get('product_id') or hit.get('url') for hit in hits}
                for page in range(2, max_pages + 1):
                    if len(hits) >= limit or not re.search(r'rel=[\"\']next[\"\']', response.text):
                        break
                    try:
                        response = await client.get(url + f'&pg={page}')
                    except httpx.HTTPError:
                        break
                    if response.status_code != 200:
                        break
                    fresh = [h for h in parse_storefront_search_html(response.text)
                             if (h.get('product_id') or h.get('url')) not in seen]
                    if not fresh:
                        break
                    hits.extend(fresh)
                    seen.update(h.get('product_id') or h.get('url') for h in fresh)
                has_next = bool(re.search(r'rel=["\']next["\']', response.text))
                complete = not has_next and len(hits) <= limit
                return StorefrontSearchResults(hits[:limit], complete=complete)
    log_event("story_storefront_search", {"query": q[:40], "hits": 0})
    return StorefrontSearchResults()


async def hydrate_storefront_hits(
    hits: list[dict[str, str]],
    *,
    execute_tool: Any,
) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        product_id = str(hit.get("product_id") or "").strip()
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        product: dict[str, Any] | None = None
        try:
            result = await execute_tool("get_product", {"product_id": product_id})
        except Exception:
            result = None
        if isinstance(result, dict) and not result.get("error"):
            product = dict(result)
            product["id"] = str(product.get("id") or product_id)
            if hit.get("url") and not product.get("url"):
                product["url"] = hit["url"]
        else:
            product = _product_from_storefront_hit(hit)
            log_event(
                "story_storefront_hydrate_fallback",
                {"product_id": product_id, "name": hit.get("name", "")[:80]},
            )
        if product:
            products.append(product)
    return products
