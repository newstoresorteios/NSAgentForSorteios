"""Validate the URLs actually delivered, after all generative rewrites."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse
from app.configuration.runtime import policy, message, ConfigurationUnavailable
from app.catalog.media.storefront_evidence import json_policy
from app.catalog.media.product_media import ensure_product_has_live_url, official_product_url


async def validate_catalog_delivery(result):
    try:
        hosts = set(json_policy('storefrontHosts'))
    except ConfigurationUnavailable:
        hosts = set()
    data = result.commercial_data or {}
    products = [p for p in data.get('products', []) if isinstance(p, dict)]
    link = data.get('product_link') or {}
    if link.get('product_url'):
        products = [*products, {'id': link.get('product_id'), 'url': link['product_url']}]
    urls = list(dict.fromkeys(u.rstrip('.,;!*') for u in re.findall(r'https?://[^\s<>\])]+', result.reply_text or '')
                             if urlparse(u).hostname in hosts and 'relogio' in urlparse(u).path.lower()))
    validated = result.response_metadata.setdefault('validated_catalog_urls', {})

    async def check(url):
        product = next((p for p in products if url in (p.get('url'), p.get('product_url'))), {})
        pid = str(product.get('id') or '')
        if not pid:
            return url, None, pid
        if validated.get(url) == pid:
            return url, url, pid
        # Verification performed in this turn by the visual resolver is sufficient.
        if product.get('_product_url_verified') and product.get('_image_identity_verified'):
            return url, url, pid
        checked = await ensure_product_has_live_url({'id': pid, 'url': url})
        return url, official_product_url(checked), pid

    checks = await asyncio.gather(*(check(url) for url in urls))
    bad = [url for url, live, _ in checks if not live]
    if bad:
        # Keep catalog facts for analysis but remove rejected URLs from every
        # outbound/memory source so a later composer cannot resurrect them.
        for product in products:
            if product.get('url') in bad or product.get('product_url') in bad:
                product.update(url=None, product_url=None, _product_url_dead=True)
        if link.get('product_url') in bad:
            link.update(product_url=None, product_url_dead=True)
        result.reply_text = message('catalog_link_unverified')
        result.safety_reason = 'catalog_link_unverified'
        result.response_metadata.update(clear_active_product=True, clear_presented_products=True,
                                        presented_products=False, rejected_catalog_urls=bad)
    else:
        for url, live, pid in checks:
            result.reply_text = result.reply_text.replace(url, live)
            validated[live] = pid
    return result


def enforce_photo_identity(result):
    """An image reviewer may not replace an uncertain draft with a catalog sibling."""
    md = result.response_metadata
    if not md.get('image_evidence_guard'):
        return result
    proof = md.get('image_catalog_proof') or {}
    products = (result.commercial_data or {}).get('products') or []
    confirmed = [p for p in products if str(p.get('id')) == proof.get('product_id')
                 and p.get('_image_identity_verified') and p.get('_product_url_verified')
                 and p.get('url') == proof.get('url') and not p.get('_product_url_dead')]
    if len(confirmed) == 1 and not md.get('rejected_catalog_urls'):
        p = confirmed[0]
        key = 'image_catalog_identified_unavailable' if p.get('available') is False else 'image_catalog_identified'
        result.reply_text = message(key, name=p['name'], url=p['url'])
        result.commercial_data = {'products': confirmed, 'match_status': 'exact'}
        md.update(presented_products=True, clear_presented_products=False,
                  product_resolution_state='resolved')
    else:
        result.reply_text = message('image_catalog_unconfirmed')
        result.commercial_data = {'products': [], 'match_status': 'unresolved'}
        result.safety_reason = 'image_catalog_unconfirmed'
        md.update(presented_products=False, clear_presented_products=True,
                  product_resolution_state='unresolved')
    md.pop('active_product', None)
    md.pop('activate_first_product', None)
    md.update(clear_active_product=True, clear_pending_action=True)
    return result


def apply_output_style(result):
    try:
        if policy('responsePlainTextEnabled'):
            result.reply_text = result.reply_text.replace('*', '')
    except ConfigurationUnavailable:
        pass
    return result
