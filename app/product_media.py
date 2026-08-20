from __future__ import annotations

import re
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse, urlunparse

from .commerce_context import CommerceProductReference
from .models import AgentResult


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

_DEAD_STOREFRONT_MARKERS = (
    "pagenotfound",
    "no_results",
    "sem-resultados",
    "sem_resultados",
)
_BRACELET_SUFFIXES = (
    "vk",
    "vc",
    "hko",
    "hb",
    "b0",
    "b1",
    "sg",
    "sb",
    "rk",
    "hk",
)
_SPELLING_SWAPS = (
    ("seander", "sealander"),
    ("sealander", "seander"),
    ("selander", "sealander"),
    ("sealander", "selander"),
)
_PATH_SWAPS = (
    ("/relogios-christopher-ward/", "/christopher-ward/"),
    ("/christopher-ward/", "/relogios-christopher-ward/"),
)
_HOST_SWAPS = (
    ("www.newstorerj.com.br", "www.newstorerj.com"),
    ("www.newstorerj.com", "www.newstorerj.com.br"),
    ("newstorerj.com.br", "newstorerj.com"),
    ("newstorerj.com", "newstorerj.com.br"),
)


def _https_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme == "https" and parsed.netloc:
        return candidate
    return None


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme == "http" and parsed.netloc:
        return candidate
    return None


def official_product_url(product: dict[str, Any]) -> str | None:
    """Normalize the current string contract and the legacy protocol map."""
    value = product.get("url")
    direct_https = _https_url(value)
    if direct_https:
        return direct_https
    if isinstance(value, dict):
        for key in ("https", "url", "link"):
            found = _https_url(value.get(key))
            if found:
                return found
        direct_http = _http_url(value.get("http"))
        if direct_http:
            return direct_http
        for key in ("url", "link"):
            found = _http_url(value.get(key))
            if found:
                return found
    return _http_url(value)


def _is_probeable_storefront(url: str) -> bool:
    host = (urlparse(url).netloc or "").casefold()
    if not host:
        return False
    if host.endswith(".example") or host == "example.com" or "localhost" in host:
        return False
    return "newstorerj." in host or host.endswith(".com.br")


def _is_dead_storefront_location(location: str | None) -> bool:
    text = (location or "").casefold()
    return any(marker in text for marker in _DEAD_STOREFRONT_MARKERS)


def storefront_url_candidates(url: str) -> list[str]:
    """Generate alternate storefront slugs when Tray returns a stale path."""
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return []
    path = parsed.path or ""
    variants: list[str] = [url.strip()]
    seen = {url.strip()}

    def _add(path_value: str) -> None:
        rebuilt = urlunparse(
            (parsed.scheme, parsed.netloc, path_value, "", parsed.query, "")
        )
        if rebuilt not in seen:
            seen.add(rebuilt)
            variants.append(rebuilt)

    for old, new in _SPELLING_SWAPS:
        if old in path.casefold():
            # Case-insensitive replace preserving path casing loosely.
            pattern = re.compile(re.escape(old), re.IGNORECASE)
            _add(pattern.sub(new, path, count=1))
    for old, new in _PATH_SWAPS:
        if old in path:
            _add(path.replace(old, new, 1))
    for old_host, new_host in _HOST_SWAPS:
        if parsed.netloc.casefold() == old_host.casefold():
            rebuilt = urlunparse(
                (parsed.scheme, new_host, path, "", parsed.query, "")
            )
            if rebuilt not in seen:
                seen.add(rebuilt)
                variants.append(rebuilt)

    # Bracelet / strap SKU suffixes often differ while the watch is the same listing family.
    match = re.search(r"-(%s)$" % "|".join(_BRACELET_SUFFIXES), path, re.IGNORECASE)
    if match:
        prefix = path[: match.start()]
        current = match.group(1).casefold()
        for suffix in _BRACELET_SUFFIXES:
            if suffix == current:
                continue
            _add(f"{prefix}-{suffix}")
    return variants


async def resolve_live_product_url(url: str | None) -> str | None:
    """Return a storefront URL that does not soft-404, or None if all candidates fail."""
    if not isinstance(url, str) or not url.strip():
        return None
    primary = url.strip()
    if not _is_probeable_storefront(primary):
        return primary

    import httpx

    candidates = storefront_url_candidates(primary)
    timeout = httpx.Timeout(4.0, connect=2.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": "NSAgentForSorteios/product-link"},
    ) as client:
        for candidate in candidates:
            try:
                response = await client.head(candidate)
            except httpx.HTTPError:
                continue
            location = response.headers.get("location")
            if response.status_code in {301, 302, 303, 307, 308} and _is_dead_storefront_location(
                location
            ):
                continue
            if response.status_code == 404:
                continue
            if response.status_code in {301, 302, 303, 307, 308} and location:
                # Follow one hop to a same-host product page; reject search dead-ends.
                if _is_dead_storefront_location(location):
                    continue
                try:
                    followed = await client.head(
                        location
                        if location.startswith("http")
                        else f"{urlparse(candidate).scheme}://{urlparse(candidate).netloc}{location}"
                    )
                except httpx.HTTPError:
                    continue
                if followed.status_code == 200 and not _is_dead_storefront_location(
                    followed.headers.get("location")
                ):
                    return candidate
                continue
            if 200 <= response.status_code < 300:
                return candidate
            # Some storefronts reject HEAD; fall back to GET without body drain cost.
            if response.status_code in {405, 501}:
                try:
                    get_response = await client.get(candidate)
                except httpx.HTTPError:
                    continue
                if get_response.status_code == 200 and not _is_dead_storefront_location(
                    get_response.headers.get("location")
                ):
                    return candidate
    return None


async def ensure_product_has_live_url(product: dict[str, Any]) -> dict[str, Any]:
    """Mutate a copy with a working storefront URL when Tray's slug is stale."""
    patched = dict(product)
    raw = official_product_url(patched)
    live = await resolve_live_product_url(raw)
    if live:
        patched["url"] = live
        if live != raw:
            patched["_product_url_repaired"] = True
            patched["_product_url_original"] = raw
    else:
        patched["_product_url_dead"] = True
    return patched


def _image_url(value: Any) -> str | None:
    direct = _https_url(value)
    if direct:
        return direct
    if isinstance(value, list):
        for item in value:
            found = _image_url(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("url", "src", "link", "https", "image_url"):
            found = _image_url(value.get(key))
            if found:
                return found
    return None


def official_product_image(product: dict[str, Any]) -> str | None:
    for key in (
        "primary_image_url",
        "primary_image",
        "image_url",
        "image",
        "images",
    ):
        found = _image_url(product.get(key))
        if found:
            return found
    return None


async def resolve_product_image(
    *,
    product_reference: CommerceProductReference,
    execute: ToolExecutor,
) -> AgentResult:
    product = await execute(
        "get_product",
        {"product_id": product_reference.product_id},
    )
    if "error" in product:
        return AgentResult(
            reply_text="Não consegui consultar a imagem oficial deste produto agora.",
            intent="commerce",
            handoff_required=False,
            safety_reason="product_media_technical_failure",
            response_metadata={
                "domain": "commerce",
                "image_url_found": False,
                "media_send_supported": False,
                "media_send_failed": False,
                "used_tray": True,
            },
        )

    image_source = "product"
    image_url = None
    if product_reference.variant_id:
        variant = await execute(
            "get_product_variant",
            {"variant_id": product_reference.variant_id},
        )
        if "error" not in variant:
            image_url = official_product_image(variant)
            if image_url:
                image_source = "variant"
    image_url = image_url or official_product_image(product)
    print("[sales.image.resolve]", {
        "has_image": bool(image_url),
        "image_source": image_source if image_url else None,
    })
    active = product_reference.model_copy(update={
        "name": product.get("name") or product_reference.name,
        "reference": product.get("reference") or product_reference.reference,
    })
    if not image_url:
        return AgentResult(
            reply_text="A Tray não informou uma imagem oficial para este produto.",
            intent="commerce",
            handoff_required=False,
            safety_reason="product_image_not_available",
            commercial_data={"products": [product], "image": None},
            response_metadata={
                "domain": "commerce",
                "active_product": active.model_dump(mode="json"),
                "image_url_found": False,
                "media_send_supported": False,
                "media_send_failed": False,
                "used_tray": True,
            },
        )
    name = str(product.get("name") or product_reference.name or "produto")
    return AgentResult(
        reply_text=f"Esta é a imagem oficial de {name}:\n{image_url}",
        intent="commerce",
        handoff_required=False,
        commercial_data={
            "products": [product],
            "image": {"url": image_url, "source": image_source},
        },
        response_metadata={
            "domain": "commerce",
            "active_product": active.model_dump(mode="json"),
            "outbound_image_url": image_url,
            "image_url_found": True,
            "media_send_supported": False,
            "media_send_failed": False,
            "media_supported": False,
            "used_tray": True,
        },
    )


async def resolve_presented_product_images(
    *,
    product_references: list[CommerceProductReference],
    execute: ToolExecutor,
) -> AgentResult:
    if not product_references:
        return AgentResult(
            reply_text="Não tenho os modelos da lista anterior para enviar a foto.",
            intent="commerce",
            handoff_required=False,
            safety_reason="product_context_missing",
            response_metadata={"domain": "commerce"},
        )
    if len(product_references) == 1:
        return await resolve_product_image(
            product_reference=product_references[0],
            execute=execute,
        )
    lines: list[str] = []
    products: list[dict[str, Any]] = []
    image_urls: list[str] = []
    technical_failure = False
    for index, reference in enumerate(product_references[:3], start=1):
        one = await resolve_product_image(product_reference=reference, execute=execute)
        payload = (one.commercial_data or {}).get("products") or []
        product = payload[0] if payload and isinstance(payload[0], dict) else {}
        if product:
            products.append(product)
        name = str(product.get("name") or reference.name or f"opção {index}")
        image_url = (one.response_metadata or {}).get("outbound_image_url")
        if one.safety_reason == "product_media_technical_failure":
            technical_failure = True
            lines.append(f"{index}. {name}: não consegui consultar a imagem agora.")
            continue
        if isinstance(image_url, str) and image_url.strip():
            image_urls.append(image_url)
            lines.append(f"{index}. {name}\n{image_url}")
            continue
        lines.append(f"{index}. {name}: a Tray não informou uma imagem oficial.")
    first_url = image_urls[0] if image_urls else None
    safety = None
    if not image_urls:
        safety = (
            "product_media_technical_failure"
            if technical_failure
            else "product_image_not_available"
        )
    return AgentResult(
        reply_text="Estas são as fotos oficiais dos modelos que listei:\n" + "\n".join(lines),
        intent="commerce",
        handoff_required=False,
        safety_reason=safety,
        commercial_data={"products": products, "images": image_urls},
        response_metadata={
            "domain": "commerce",
            "presented_products": True,
            "outbound_image_url": first_url,
            "outbound_image_urls": image_urls,
            "image_url_found": bool(first_url),
            "media_send_supported": False,
            "media_send_failed": False,
            "used_tray": True,
        },
    )
