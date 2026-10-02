"""Compact full-catalog evidence; no ranking model and no first-page shortlist."""
import asyncio
import hashlib


def candidate_id(product):
    return hashlib.sha256(product['url'].encode()).hexdigest()[:16]


async def all_ready_products(adapter, *, snapshot_id=None):
    products = {}
    offset = 0
    expected_total = None
    async with asyncio.timeout(35):
        while True:
            page = await adapter.search_ready_delivery('pronta entrega', offset=offset, limit=50,
                                                        snapshot_id=snapshot_id)
            if not isinstance(page, dict) or not page.get('complete') or not page.get('snapshot_id'):
                raise ValueError('catalog_overview_incomplete')
            if snapshot_id is not None and snapshot_id != page['snapshot_id']:
                raise ValueError('catalog_snapshot_changed')
            snapshot_id = page['snapshot_id']
            total = page.get('total')
            if type(total) is not int or not 0 <= total <= 500:
                raise ValueError('catalog_overview_too_large')
            if expected_total is not None and total != expected_total:
                raise ValueError('catalog_total_changed')
            expected_total = total
            for product in page.get('products', []):
                if product.get('url'):
                    products[candidate_id(product)] = product
            if not page.get('has_more'):
                if len(products) != total:
                    raise ValueError('catalog_overview_incomplete')
                return products, snapshot_id, page.get('checkedAt')
            next_offset = page.get('next_offset')
            if type(next_offset) is not int or not offset < next_offset < total:
                raise ValueError('catalog_overview_incomplete')
            offset = next_offset


def compact_candidates(products):
    # URLs/photos stay in the backend; only comparable facts are sent in bulk.
    return [{'candidate_id': key, 'name': str(p.get('name') or '')[:250],
             'reference': str(p.get('reference') or '')[:80]}
            for key, p in products.items()]
