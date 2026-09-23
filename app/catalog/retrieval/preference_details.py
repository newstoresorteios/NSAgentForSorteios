"""Bounded evidence recovery before filtering explicit size/strap requests."""
from app.catalog.specs.catalog_specs import interpretation_case_size_range, extract_case_size_mm
from app.catalog.retrieval.hard_filter import _required_strap_material, product_matches_required_strap_material
from app.sales.contextual_discovery import configuration


async def confirm_preference_details(session):
    config = configuration()
    if not config:
        return
    size = interpretation_case_size_range(session.interpretation, message_text=session.message_text)
    strap = _required_strap_material(session.interpretation)
    if not (size or strap):
        return
    from app.catalog.retrieval.rerank import deterministic_semantic_order

    checked = 0
    replacements = {}
    for product in deterministic_semantic_order(session.candidates, session.interpretation):
        needs_detail = (size and not extract_case_size_mm(product)) or (
            strap and not product_matches_required_strap_material(product, strap)
        )
        if not needs_detail or product.get("id") is None:
            continue
        if checked >= config["detailLimit"]:
            break
        checked += 1
        try:
            live = await session.execute_tool("get_product", {"product_id": str(product["id"])})
        except Exception as exc:
            # Optional recovery must not discard the original candidate pool.
            print("[catalog.preference_details.failed]", {"error_type": type(exc).__name__})
            break
        if not isinstance(live, dict) or live.get("error"):
            continue
        current = {**live, "id": product["id"]}
        if strap and not product_matches_required_strap_material(current, strap):
            try:
                variants = await session.execute_tool("list_product_variants", {"product_id": str(product["id"])})
            except Exception as exc:
                print("[catalog.preference_details.variants_failed]", {"error_type": type(exc).__name__})
                variants = {}
            if isinstance(variants, dict) and isinstance(variants.get("variants"), list):
                current["variants"] = variants["variants"]
        replacements[str(product["id"])] = current
    session.candidates = [replacements.get(str(p.get("id")), p) for p in session.candidates]
    print("[catalog.preference_details]", {"checked": checked, "refreshed": len(replacements)})
