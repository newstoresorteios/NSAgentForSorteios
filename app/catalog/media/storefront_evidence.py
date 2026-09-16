"""Structured, current storefront facts. Search results are not product proof."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse
from app.configuration.runtime import policy, ConfigurationUnavailable


def json_policy(key, expected_type=list):
    value = policy(key)
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, expected_type) or not all(isinstance(v, str) for v in value):
        raise ConfigurationUnavailable('Invalid structured policy: ' + key)
    if isinstance(value, dict) and not all(isinstance(v, str) for v in value.values()):
        raise ConfigurationUnavailable('Invalid policy values: ' + key)
    return value


def storefront_data(html: str) -> list[dict]:
    rows = []
    for match in re.finditer(r'\bdataLayer\s*=\s*', html or ''):
        try:
            value, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
        except ValueError:
            continue
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    return rows


def product_page_evidence(html: str, *, expected_id: str | None = None) -> dict | None:
    """Only the page's product, never recommendations or hidden modal copy."""
    rows = [r for r in storefront_data(html)
            if r.get('idProduct') and r.get('nameProduct') and not r.get('listProducts')]
    if len(rows) != 1:
        return None
    row = rows[0]
    if expected_id and str(row['idProduct']) != str(expected_id):
        return None
    url = str(row.get('urlProduct') or '').replace('http://', 'https://', 1)
    if urlparse(url).scheme != 'https':
        return None
    flag = str(row.get('availability') or '').upper()
    return {
        'id': str(row['idProduct']), 'name': row['nameProduct'], 'url': url,
        'reference': row.get('reference') or None, 'model': row.get('model') or None,
        'brand': row.get('brand') or None, 'image_url': row.get('urlImage'),
        'available': True if flag == 'YES' else False if flag == 'NO' else None,
        'availability': row.get('availabilityDetails') or None,
        'price': row.get('priceSell') or row.get('price'),
    }


async def fetch_storefront_product(url: str, *, expected_id: str) -> dict | None:
    import httpx
    hosts = set(json_policy('storefrontHosts'))
    if urlparse(url).hostname not in hosts:
        return None
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        response = await client.get(url)
    if response.status_code != 200 or urlparse(str(response.url)).hostname not in hosts:
        return None
    evidence = product_page_evidence(response.text, expected_id=expected_id)
    if not evidence or urlparse(evidence['url']).hostname not in hosts:
        return None
    evidence['url'] = str(response.url)
    return evidence


def image_search_queries(identified) -> list[str]:
    import unicodedata
    def fold(text):
        return ''.join(c for c in unicodedata.normalize('NFKD', str(text or '').lower())
                       if not unicodedata.combining(c))
    aliases = json_policy('imageSearchColorAliases', dict)
    ignored = set(json_policy('imageSearchGenericTokens')) | set(aliases) | set(aliases.values())
    brand = fold(identified.brand)
    tokens = re.findall(r'[\w-]+', fold(identified.model))
    core = ' '.join(t for t in tokens if t not in ignored and t not in brand.split())
    colors = re.findall(r'\w+', fold(identified.color))
    color = next((aliases[t] for t in colors if t in aliases), '')
    # The model is a search hypothesis; never synthesize a SKU or replace a family.
    candidates = [f'{brand} {core} {color}', f'{brand} {core}', f'{brand} {color}']
    return list(dict.fromkeys(' '.join(q.split()) for q in candidates if len(q.strip()) >= 3))
