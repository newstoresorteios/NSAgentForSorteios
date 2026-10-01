"""Combine read-only SKU inspection with published policy, without inventing offers."""
from __future__ import annotations

import re

from app.catalog.retrieval.text import fold_text


def product_policy_topics(text, interpretation):
    if (interpretation is None or interpretation.domain != "commerce"
            or interpretation.goal == "after_sales" or interpretation.order_action
            or interpretation.shipping_action or interpretation.purchase_action
            or interpretation.checkout_action or interpretation.purchase_items
            or interpretation.confirmation == "confirm"):
        return set()
    from app.ops.handoff_consent import customer_requests_human
    if customer_requests_human(text):
        return set()
    query = fold_text(text)
    topics = set()
    if re.search(r"\bgarantia\b", query):
        topics.add("warranty")
    if re.search(r"\b(?:documentac|documentos|nota fiscal|certificado|manual)", query):
        topics.add("documentation")
    return topics


def normalize_product_policy_lookup(text, interpretation):
    """A named product plus a policy question must still consult the catalog."""
    if not product_policy_topics(text, interpretation):
        return interpretation
    from app.sales.intent_router import has_compiled_lookup_identity
    from app.sales.purchase_selection import is_product_information_question
    if not has_compiled_lookup_identity(interpretation) or not is_product_information_question(text):
        return interpretation
    updated = interpretation.model_copy(deep=True)
    updated.goal = "inspect"
    updated.answer_strategy = "search_catalog"
    updated.ready_for_retrieval = updated.enough_information_to_search = True
    updated.needs_clarification = False
    updated.clarification_question = None
    updated.information_needed = list(dict.fromkeys([*updated.information_needed, "catalog"]))
    return updated


def _coverage_excerpt(doc):
    """Use only the published coverage paragraph, not agent rules or exclusions."""
    body = str(doc.get("body") or "").replace("\\n", "\n")
    section = re.search(r"(?im)^##\s+Cobertura\s*\n+([^#]+)", body)
    return section.group(1).strip().split("\n\n", 1)[0] if section else None


def complete_product_policy_answer(result, interpretation, text):
    topics = product_policy_topics(text, interpretation)
    if not topics or result.handoff_required or result.response_metadata.get("image_evidence_guard"):
        return result
    # No broad bypass for generated copy: validate the offer first, then compose
    # from its approved products plus literal policy/labelled catalog evidence.
    from app.catalog.retrieval.offer_contract import enforce_offer, seal_offer
    from app.persona.store_knowledge import fetch_institutional_knowledge
    from app.sales.inspection_copy import labelled_fact
    original = result
    result = enforce_offer(result.model_copy(deep=True), interpretation)
    products = (result.commercial_data or {}).get("products") or []
    if products and not all(p.get("_revalidated") for p in products):
        return original
    if (not products and not result.response_metadata.get("offer_contract")
            and not result.response_metadata.get("used_tray")):
        return original
    evidence = fetch_institutional_knowledge(text).items
    coverage = [(doc, _coverage_excerpt(doc)) for doc in evidence
                if doc.get("slug") == "garantia-e-cuidados"]
    lines = []
    used_evidence = []
    product = products[0] if len(products) == 1 else None
    if "warranty" in topics:
        for doc, excerpt in coverage:
            if excerpt:
                lines.append("Sobre a política geral de garantia: " + excerpt)
                used_evidence.append(doc)
        warranty = labelled_fact(product, ("warranty",), {"garantia", "warranty"}) if product else None
        if warranty:
            lines.append("Garantia informada na ficha deste produto: " + warranty + ".")
        # A generic duration is NOT evidence of manufacturer activation.
        if product and product.get("manufacturer_warranty_active") is True:
            lines.append("A ficha consultada informa garantia ativa do fabricante para este produto.")
        else:
            lines.append("A garantia ativa diretamente com o fabricante não está confirmada nos dados consultados.")
    if "documentation" in topics:
        documents = labelled_fact(product, ("included_documents",),
            {"documentacao inclusa", "documentos inclusos", "included documents"}) if product else None
        lines.append("Documentação informada na ficha: " + documents + "." if documents else
            "A lista completa de documentos que acompanha esta peça não está confirmada na ficha consultada.")
    if not lines:
        return result
    lines.append("Posso pedir à equipe a confirmação desses pontos específicos antes da compra.")
    # Reconstruct the commercial portion; never preserve unsupported free-form
    # promises, prices or references from an LLM rewrite of a rejected offer.
    from app.commerce.commerce_router import _product_result
    contract = result.response_metadata.get("offer_contract") or {}
    base = (_product_result("product_search", products).reply_text if products
            else str(contract.get("reply_text") or result.reply_text))
    if products:
        from app.sales.inspection_copy import complete_inspection_copy
        inspected = complete_inspection_copy(result, interpretation, text)
        if inspected is not result:
            base = inspected.reply_text
    prior = result.response_metadata.get("product_policy_answer") or {}
    if prior.get("reply_text") == base:
        base = prior["catalog_reply"]
    result.reply_text = base + "\n\n" + "\n\n".join(lines)
    result.response_metadata.update(
        institutional_evidence=used_evidence,
        product_policy_answer={"catalog_reply": base, "reply_text": result.reply_text,
                               "topics": sorted(topics)},
        response_source="grounded_product_policy",
        used_openai_responder=False,
    )
    # Seal the composed answer too. Later rewrites cannot erase the policy or
    # revive a rejected SKU. All existing price/stock/identity checks still run.
    return seal_offer(result)
