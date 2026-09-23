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
    from app.catalog.retrieval.price import effective_price

    # Rank within budget groups before semantic ordering truncates its pool.
    # Otherwise high-scoring, over-budget watches consume every detail slot,
    # while a compatible promotional-price candidate never gets its sheet read.
    prefs = session.interpretation.preferences
    groups = [[], [], []]  # compatible known price, unknown, outside budget
    for product in session.candidates:
        price = effective_price(product)
        if price is None:
            group = 1
        elif ((prefs.budget_max is not None and price > prefs.budget_max)
              or (prefs.budget_min is not None and price < prefs.budget_min)):
            group = 2
        else:
            group = 0
        groups[group].append(product)
    ordered = [product for group in groups
               for product in deterministic_semantic_order(group, session.interpretation)]

    checked = 0
    replacements = {}
    for product in ordered:
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
