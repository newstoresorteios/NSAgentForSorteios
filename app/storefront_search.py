"""Search the New Store vitrine when Tray name search misses a listing."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

from .observability import log_event

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


def _decode_js_string(raw: str) -> str:
    text = raw.replace(r"\/", "/")
    try:
        return bytes(text, "utf-8").decode("unicode_escape")
    except Exception:
        return text


def parse_storefront_search_html(html: str) -> list[dict[str, str]]:
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
        try:
            result = await execute_tool("get_product", {"product_id": product_id})
        except Exception:
            continue
        if not isinstance(result, dict) or result.get("error"):
            continue
        product = dict(result)
        product["id"] = str(product.get("id") or product_id)
        if hit.get("url") and not product.get("url"):
            product["url"] = hit["url"]
        products.append(product)
    return products
