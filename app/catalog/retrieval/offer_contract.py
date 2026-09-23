"""Keep validated retrieval facts authoritative across response rewrites.

This internal snapshot is scoped to one turn, never a persistent product lock.
Operator-managed copy is captured after retrieval, not duplicated here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, Field


def commercial_snapshot(products):
    """Ignore audit enrichment while retaining identity, specifications and price."""
    from .price import resolve_commercial_price
    if products and all(p.get('_revalidated') for p in products):
        # The factual authorizer legitimately removes unapproved presentation
        # fields (image lists, category labels, payment blurbs). Compare the same
        # authorized projection on both sides, not a full sheet to its projection.
        from app.verify.fact_authority import authorize_products_for_responder
        products, _ = authorize_products_for_responder(products)
    rows = []
    for product in products:
        facts = {k:v for k,v in product.items()
                 if not k.startswith('_') and k not in {
                     'tenant_id','commercial_availability','price','promotional_price','current_price',
                     'url','product_url'}}
        facts['id'] = str(product.get('id') or '')
        facts['effective_price'] = str(resolve_commercial_price(product, require_positive=True).amount)
        facts['url'] = product.get('url') or product.get('product_url')
        rows.append(facts)
    return rows


class OfferContract(BaseModel):
    version: Literal[1] = 1
    products: list[dict[str, Any]] = Field(default_factory=list)
    reply_text: str
    safety_reason: str | None = None
    rejected_candidates: list[dict[str, Any]] = Field(default_factory=list)
    missing_evidence: list[dict[str, Any]] = Field(default_factory=list)
    integration_errors: list[dict[str, Any]] = Field(default_factory=list)


def seal_offer(result):
    approved_ids = {str(p.get('id')) for p in (result.commercial_data or {}).get('products') or []}
    if 'rejected_candidates' not in result.response_metadata:
        rejected = []
        for row in result.response_metadata.get('technical_evidence', []):
            if row.get('stage') != 'live_detail' or str(row.get('product_id')) in approved_ids:
                continue
            rejected.append({'product_id':str(row.get('product_id')), 'status':row.get('status'),
                             'commercial':row.get('commercial'), 'source':'tray_live'})
        result.response_metadata['rejected_candidates'] = rejected
        result.response_metadata['missing_evidence'] = [row for row in rejected if row['status']=='unknown'
            or (row.get('commercial') or {}).get('price_status')=='missing']
    contract = OfferContract(
        products=deepcopy((result.commercial_data or {}).get("products") or []),
        reply_text=result.reply_text,
        safety_reason=result.safety_reason,
        rejected_candidates=deepcopy(result.response_metadata.get('rejected_candidates', [])),
        missing_evidence=deepcopy(result.response_metadata.get('missing_evidence', [])),
        integration_errors=deepcopy(result.response_metadata.get('integration_errors', [])),
    )
    result.response_metadata["offer_contract"] = contract.model_dump(mode="json")
    return result


def validate_recommendation(result, interpretation, *, force=False):
    """Shared admission for initial and reviewer retrieval, not known-SKU inspection."""
    from app.configuration.runtime import current_bundle
    if not (force or current_bundle().get('values', {}).get('approvedOfferContractEnabled')):
        return result
    if not interpretation or interpretation.goal not in {'find', 'recommend', 'discover'}:
        return result
    if result.response_metadata.get('variant_refinement') or result.handoff_required:
        return result
    from .price import resolve_commercial_price
    from .availability import product_availability_state
    from .hard_filter import hard_filter_products
    from app.catalog.specs.requirements import technical_requirements, feature_evidence
    products = (result.commercial_data or {}).get('products') or []
    if not products:
        return result
    accepted, rejected, missing = [], [], []
    required = technical_requirements(interpretation)
    for product in products:
        reasons = []
        if not product.get('id') or not product.get('name'):
            reasons.append('identity_missing')
        if not product.get('_revalidated'):
            reasons.append('live_confirmation_missing')
        if resolve_commercial_price(product, require_positive=True).amount is None:
            reasons.append('price_missing')
        availability = product_availability_state(product)
        if availability != 'available':
            reasons.append('availability_' + availability)
        if required and feature_evidence(product, required)['status'] != 'matched':
            reasons.append('technical_evidence_missing_or_mismatch')
        if not hard_filter_products([product], interpretation, mode='recommendation'):
            reasons.append('constraints_mismatch')
        if reasons:
            row = {'product_id':str(product.get('id') or ''), 'reasons':reasons,
                   'source':product.get('_factual_source'), 'checked_at':product.get('_freshness_at')}
            rejected.append(row)
            if any('missing' in reason or 'unknown' in reason for reason in reasons):
                missing.append(row)
        else:
            accepted.append(product)
    if rejected:
        result = result.model_copy(deep=True)
        if accepted:
            from app.commerce.commerce_router import _product_result
            result.reply_text = _product_result('product_search', accepted).reply_text
            result.commercial_data = {**(result.commercial_data or {}), 'products':accepted}
        else:
            from .technical import technical_miss
            replacement = technical_miss(interpretation, unknown=bool(missing))
            result.reply_text, result.safety_reason = replacement.reply_text, replacement.safety_reason
            result.commercial_data = {'products':[]}
            result.response_metadata.update(replacement.response_metadata)
        result.response_metadata.pop('active_product', None)
        result.response_metadata.pop('activate_first_product', None)
    result.response_metadata.update(rejected_candidates=rejected, missing_evidence=missing)
    from app.catalog.index.catalog_index import build_allowed_id_sets
    result.response_metadata['allowed_id_sets'] = {k:sorted(v) for k,v in build_allowed_id_sets(accepted).items()}
    return seal_offer(result)


def enforce_offer(result, interpretation=None):
    """A reviewer may improve wording, but cannot silently replace live facts."""
    raw = result.response_metadata.get("offer_contract")
    if raw is None or result.handoff_required:
        return result
    contract = OfferContract.model_validate(raw)
    products = (result.commercial_data or {}).get("products") or []
    same_facts = commercial_snapshot(products) == commercial_snapshot(contract.products)
    if same_facts and contract.products:
        return result
    if not same_facts and contract.products:
        # Changed facts need another retrieval validation, not restoration of an
        # earlier price/stock snapshot that may now be obsolete.
        from app.catalog.retrieval.technical import technical_miss
        replacement = technical_miss(interpretation, unknown=True)
        result.reply_text = replacement.reply_text
        result.safety_reason = replacement.safety_reason
        result.commercial_data = replacement.commercial_data
        result.response_metadata.update(replacement.response_metadata)
        result.response_metadata["offer_contract_restored"] = True
        result.response_metadata.pop("active_product", None)
        result.response_metadata.pop("activate_first_product", None)
        return result
    # No approved offers: even a text-only rewrite must not revive a rejected SKU.
    changed = products != contract.products or result.reply_text != contract.reply_text
    result.commercial_data = {**(result.commercial_data or {}), "products": deepcopy(contract.products)}
    result.reply_text = contract.reply_text
    result.safety_reason = contract.safety_reason
    metadata = result.response_metadata
    metadata["offer_contract_restored"] = changed
    metadata.pop("active_product", None)
    metadata.pop("activate_first_product", None)
    metadata["presented_products"] = bool(contract.products)
    from app.catalog.index.catalog_index import build_allowed_id_sets
    metadata["allowed_id_sets"] = {k: sorted(v) for k, v in build_allowed_id_sets(contract.products).items()}
    if not contract.products:
        metadata.update(clear_active_product=True, clear_presented_products=True)
    return result


def responder_evidence(metadata):
    """Retain full rejected evidence in logs, without feeding offers to the writer."""
    evidence = metadata.get("technical_evidence")
    raw = metadata.get("offer_contract")
    if raw is None:
        return evidence
    approved = {str(p.get("id")) for p in OfferContract.model_validate(raw).products}
    return [row for row in evidence or []
            if row.get("stage") == "live_detail" and str(row.get("product_id")) in approved]
