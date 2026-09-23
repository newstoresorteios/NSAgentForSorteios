"""Resolve a photo using catalog images; text hypotheses cannot prove a SKU."""
from __future__ import annotations

import httpx
from app.configuration.runtime import ConfigurationUnavailable, message as copy, policy
from app.models import AgentResult
from app.ops.observability import log_event
from app.catalog.media.storefront_evidence import fetch_storefront_product, image_search_queries
from app.catalog.media.storefront_search import search_storefront, rank_storefront_hits_by_image


async def resolve_catalog_photo(incoming, identified) -> AgentResult:
    # Defaults must exist even when configuration or the first HTTP request fails.
    search_incomplete = False
    candidates_compared = False
    comparisons = []
    ambiguous = False
    base = {
        'domain': 'commerce', 'goal': 'find', 'image_search': True,
        'response_source': 'image_catalog_evidence', 'image_evidence_guard': True,
        'image_identify': identified.model_dump(mode='json'),
        'clear_active_product': True, 'clear_presented_products': True,
        'presented_products': False, 'product_resolution_state': 'unresolved',
        # Do not persist speculative references, movements or old photo preferences.
        'active_preferences': {'subject_brand': identified.brand,
                               'subject_model': identified.model,
                               'subject_reference': None, 'color': identified.color,
                               'attributes': [], 'material': None},
    }
    try:
        enabled = policy('imageStorefrontSearchEnabled')
        limit = max(1, min(120, int(policy('imageStorefrontCandidateLimit'))))
        pages = max(1, min(10, int(policy('imageStorefrontMaxPages'))))
        max_queries = max(1, min(5, int(policy('imageStorefrontMaxQueries'))))
        distance_max = int(policy('imageStorefrontPerceptualDistanceMax'))
        margin_min = int(policy('imageStorefrontPerceptualMinMargin'))
        color_max = float(policy('imageStorefrontColorErrorMax'))
        exact_distance_max = int(policy('imageStorefrontExactDistanceMax'))
        exact_color_max = float(policy('imageStorefrontExactColorErrorMax'))
        queries = image_search_queries(identified) if enabled else []
        seen = set()
        search_incomplete = False
        candidates_compared = False
        for query in queries[:max_queries]:
            hits = await search_storefront(query, max_pages=pages, limit=limit)
            # Compare the entire bounded candidate pool, including siblings.
            ranked = await rank_storefront_hits_by_image(str(incoming.image_url), hits, limit=limit)
            if not ranked:
                log_event('image.catalog_candidates', {'query': query, 'count': len(hits), 'ranked': 0})
                continue
            candidates_compared = True
            best_distance, hit = ranked[0]
            color_error = hit.get('_image_color_error')
            exact_visual = (
                best_distance <= exact_distance_max
                and color_error is not None
                and color_error <= exact_color_max
            )
            complete = bool(getattr(hits, 'complete', len(hits) < limit))
            if not complete or len(ranked) < len(hits):
                search_incomplete = True
                log_event('image.catalog_comparison_incomplete', {'query': query, 'count': len(hits), 'ranked': len(ranked)})
                # An identical official image remains conclusive even if a broad
                # textual search has more pages. Approximate matches still wait
                # for a complete competitor set.
                if not exact_visual or len(ranked) < len(hits):
                    continue
            margin = ranked[1][0] - best_distance if len(ranked) > 1 else margin_min
            comparisons.extend({'product_id':str(candidate.get('product_id') or ''),
                'distance':distance, 'color_error':candidate.get('_image_color_error'),
                'source_view':candidate.get('_image_source_view'),
                'candidate_view':candidate.get('_image_candidate_view')}
                for distance, candidate in ranked)
            # Shared merchant photos cannot distinguish sibling SKUs even when
            # one photo is pixel-identical to the customer's upload.
            if len(ranked) > 1 and margin <= 0:
                ambiguous = True
                continue
            if ambiguous:
                # A narrower subsequent query must not hide a proven sibling.
                continue
            log_event('image.catalog_candidates', {
                'query': query, 'count': len(hits), 'ranked': len(ranked),
                'best_id': hit.get('product_id'), 'distance': best_distance,
                'margin': margin, 'color_error': color_error,
                'candidate_limit_reached': len(hits) >= limit,
            })
            if not exact_visual and (
                best_distance > distance_max or margin < margin_min
                or color_error is None or color_error > color_max
            ):
                continue
            pid = str(hit.get('product_id') or '')
            if not pid or pid in seen:
                continue
            seen.add(pid)
            product = await fetch_storefront_product(str(hit.get('url') or ''), expected_id=pid)
            if not product:
                log_event('image.catalog_page_rejected', {'product_id': pid})
                continue
            product.update(storefront_only=True, _image_identity_verified=True,
                           _product_url_verified=True, available_for_purchase=product['available'])
            # Identity and stock are separate facts; unknown stock is not sold-out.
            key = ('image_catalog_identified_unavailable' if product['available'] is False
                   else 'image_catalog_identified')
            return AgentResult(
                reply_text=copy(key, name=product['name'], url=product['url']),
                intent='commerce',
                commercial_data={'products': [product], 'match_status': 'exact'},
                response_metadata={**base, 'clear_presented_products': False,
                    'presented_products': True, 'product_resolution_state': 'resolved',
                    'image_catalog_proof': {'product_id': pid, 'distance': best_distance,
                        'margin': margin, 'color_error': color_error, 'url': product['url']},
                    'image_candidate_comparisons': comparisons,
                    'active_preferences': {'subject_brand': product.get('brand') or identified.brand,
                        'subject_model': product['model'] or product['name'],
                        'subject_reference': product['reference'], 'color': identified.color,
                        'attributes': [], 'material': None}},
            )
    except (ConfigurationUnavailable, httpx.HTTPError, OSError, ValueError, TypeError) as exc:
        log_event('image.catalog_resolution_failed', {'error_type': type(exc).__name__})
    reason = ('image_catalog_ambiguous' if ambiguous else 'image_catalog_search_incomplete' if search_incomplete
              else 'image_catalog_unconfirmed')
    return AgentResult(reply_text=copy(reason), intent='commerce',
                       safety_reason=reason,
                       commercial_data={'products': [], 'match_status': 'unresolved'},
                       response_metadata={**base, 'catalog_search_incomplete': search_incomplete,
                                          'image_candidate_comparisons': comparisons,
                                          'catalog_candidates_compared': candidates_compared})
