"""Search each visible watch independently, sharing only read-only catalog requests."""
from __future__ import annotations

import asyncio
import json

from app.stories.story_selection import analysis_for_region


async def match_scene_catalog(*, tenant_id, analysis, execute_tool):
    from app.stories.story_product_matcher import match_story_to_catalog

    requests = {}

    async def cached_read(name, arguments):
        if name not in {'search_products', 'get_product'}:
            raise ValueError('story_catalog_read_only')
        key = (name, json.dumps(arguments, sort_keys=True))
        if key not in requests:
            requests[key] = asyncio.create_task(execute_tool(name, arguments))
        return await requests[key]

    regions = analysis.product_regions
    multi = len(regions) > 1
    scopes = [analysis_for_region(analysis, region) for region in regions[:6]] if multi else [analysis]
    semaphore = asyncio.Semaphore(2)

    async def search(scoped):
        async with semaphore:
            return await match_story_to_catalog(tenant_id=tenant_id, analysis=scoped,
                execute_tool=cached_read, media_bytes=None, store_url=None, identity_search=True)

    searches = [asyncio.create_task(search(scoped)) for scoped in scopes]
    try:
        results = await asyncio.gather(*searches)
    finally:
        # Cancellation/timeout must not leave requests running beyond the job.
        for task in searches:
            if not task.done():
                task.cancel()
        await asyncio.gather(*searches, return_exceptions=True)
        for task in requests.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*requests.values(), return_exceptions=True)
    by_region = {str(i): [c.model_dump(mode='json') for c in candidates[:5]]
                 for i, candidates in enumerate(results)}
    # Round-robin keeps a dominant brand from consuming all review slots.
    merged, seen = [], set()
    for rank in range(5):
        for candidates in results:
            if rank < len(candidates) and candidates[rank].catalog_item_key not in seen:
                candidate = candidates[rank]
                seen.add(candidate.catalog_item_key)
                merged.append(candidate)
    return merged, by_region
