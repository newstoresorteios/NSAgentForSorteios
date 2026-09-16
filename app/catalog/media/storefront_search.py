"""Search the New Store vitrine when Tray name search misses a listing."""

from __future__ import annotations

from io import BytesIO
import json
import re
from typing import Any
from urllib.parse import quote_plus

from app.ops.observability import log_event

_STORE_CODE = "687890"
_HOSTS = ("www.newstorerj.com.br", "www.newstorerj.com")
_ITEM_RE = re.compile(
    r'"item_id"\s*:\s*"(\d+)"\s*,\s*"item_name"\s*:\s*"((?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)
_HREF_RE = re.compile(
    r'href="(https://www\.newstorerj\.com(?:\.br)?/(?:relogios[^/]*/relogio-|relogio-seminovo-)[^"]+)"',
    re.IGNORECASE,
)
_REF_RE = re.compile(r"(c\d{2}-\d{2}[a-z0-9-]+)", re.IGNORECASE)
_SKU_RE = re.compile(r"\b(96[a-z]\d{3})\b", re.IGNORECASE)
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
    if not reference:
        sku_match = _SKU_RE.search(name.replace(" ", " "))
        if sku_match:
            reference = sku_match.group(1).upper()
    brand = ""
    lowered = name.casefold()
    for candidate in (
        "baltic",
        "bulova",
        "tissot",
        "christopher ward",
        "citizen",
        "hamilton",
        "seiko",
    ):
        if candidate in lowered:
            brand = candidate.title() if candidate != "christopher ward" else "Christopher Ward"
            break
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


async def rank_storefront_hits_by_image(
    image_url: str,
    hits: list[dict[str, str]],
    *,
    limit: int = 8,
) -> list[tuple[int, dict[str, str]]]:
    """Rank official storefront candidates against the inbound customer image."""
    import asyncio
    import httpx

    candidates = [hit for hit in hits[: max(1, limit)] if hit.get("image_url")]
    if not image_url or not candidates:
        return []
    timeout = httpx.Timeout(10.0, connect=3.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        source_response = await client.get(image_url)
        source_response.raise_for_status()
        source_hash = perceptual_image_hash(source_response.content)

        async def _distance(hit: dict[str, str]):
            try:
                response = await client.get(str(hit["image_url"]))
                response.raise_for_status()
                return (source_hash ^ perceptual_image_hash(response.content)).bit_count(), hit
            except (httpx.HTTPError, OSError, ValueError):
                return None

        rows = await asyncio.gather(*[_distance(hit) for hit in candidates])
    ranked = [row for row in rows if row is not None]
    ranked.sort(key=lambda row: row[0])
    return ranked


async def storefront_product_available(url: str) -> bool | None:
    """Read the official product page availability without treating it as purchasable."""
    from html import unescape
    import httpx
    import unicodedata

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
    folded = "".join(
        char
        for char in unicodedata.normalize("NFKD", unescape(response.text)).casefold()
        if not unicodedata.combining(char)
    )
    if "produto indisponivel" in folded:
        return False
    return True


async def search_storefront(query: str) -> list[dict[str, str]]:
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
        for host in _HOSTS:
            url = (
                f"https://{host}/loja/busca.php?loja={_STORE_CODE}"
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
                return hits[:8]
    log_event("story_storefront_search", {"query": q[:40], "hits": 0})
    return []


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
