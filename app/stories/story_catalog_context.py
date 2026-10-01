"""Bounded visual search hints, never an assertion of product identity."""
import re

from app.catalog.retrieval.text import fold_text


COLORS = r"azul(?: claro)?|preto|prateado|prata|branco|verde|marrom|dourado|rosa|vermelho|laranja|cinza"
SIZE = r"\b\d{2}(?:[.,]\d)?\s*mm\b"


def catalog_hints(analysis):
    # Mixed-brand/model scenes must not be collapsed into one product query.
    families = analysis.collection_hypotheses or analysis.model_hypotheses
    families = list(dict.fromkeys(fold_text(v).strip() for v in families if v))
    brands = list(dict.fromkeys(fold_text(v).strip() for v in analysis.visible_brands if v))
    if len(families) != 1 or len(brands) > 1:
        return ""
    query = " ".join([*brands, families[0]])
    sizes = set(re.findall(SIZE, fold_text(" ".join(analysis.visible_text))))
    if len(sizes) == 1:
        query += " " + next(iter(sizes))
    return query[:200]


def refine_story_reference(ref, text):
    """Retain the size across 'PRX 35mm' -> 'o azul'; select only unique regions."""
    updated = dict(ref)
    value = re.sub(r"[-_\u2010-\u2015]+", " ", fold_text(text or ""))
    base = str(ref.get("catalog_query_base") or "")[:200]
    from app.stories.story_selection import selected_region_from_reference
    region_index, region = selected_region_from_reference(ref, text)
    from app.sales.ready_delivery_context import PRICE_FOLLOWUP
    if region is None and re.fullmatch(PRICE_FOLLOWUP, value.strip()):
        # Repair an existing conversation that saved a selected region with a
        # brand-only query before this fix, without requiring a new selection.
        prior_index = ref.get("selected_region_index")
        regions = ref.get("story_regions") or []
        if type(prior_index) is int and 0 <= prior_index < len(regions):
            from app.stories.instagram_story_models import VisualProductRegion
            try:
                region = VisualProductRegion.model_validate(regions[prior_index])
                region_index = prior_index
            except (TypeError, ValueError):
                pass
    if region is not None:
        label = " ".join(part.strip() for part in (
            region.brand_hypothesis, region.reference_hypothesis,
        ) if part and part.strip())
        # A brand selector in a mixed Story selects that region's model too.
        # Do not reduce "Mido Baroncelli Heritage" back to a brand-only search.
        if label:
            previous_sizes = re.findall(SIZE, base) if (
                ref.get("selected_region_index") == region_index
                or (ref.get("selected_region_index") is None
                    and fold_text(label) in fold_text(base))
            ) else []
            base = " ".join(dict.fromkeys(fold_text(label).split()))[:200]
            if len(previous_sizes) == 1 and not re.search(SIZE, base):
                base += " " + previous_sizes[0]
        updated["selected_region_index"] = region_index
        updated["selected_option"] = label or region.label
    if not base:
        # A mixed scene is searchable only when the customer names one of its
        # model hints; never concatenate every brand/model in the frame.
        named = [v for v in (ref.get("followup_terms") or []) if v in value.split()]
        if len(named) == 1:
            base = named[0]
    sizes = list(dict.fromkeys(re.findall(SIZE, value)))
    if len(sizes) == 1:
        base = re.sub(SIZE, "", base).strip() + " " + sizes[0]
    elif len(sizes) > 1:
        updated["catalog_query_base"] = ""
        updated["catalog_query"] = ""
        return updated
    options = [v for v in ref.get("clarification_options", []) if str(v).startswith("relógio ")][:5]
    color = re.findall(r"\b(?:" + COLORS + r")\b", value)
    positions = re.findall(r"\b(?:esquerda|direita|centro|cima|baixo)\b", value)
    selectors = color + positions
    if re.search(r"\b(?:nao|menos|exceto|outra|outro)\b", value):
        updated["selected_option"] = None
    elif selectors and region is None:
        normalize = lambda v: re.sub(r"\bprata\b", "prateado", fold_text(v).replace("-", " "))
        matches = [v for v in options if all(normalize(s) in normalize(v) for s in selectors)]
        updated["selected_option"] = matches[0] if len(matches) == 1 else None
    selected = updated.get("selected_option")
    selected_colors = re.findall(r"\b(?:" + COLORS + r")\b", fold_text(selected or ""))
    if region is not None and region.dial_color:
        selected_colors = [region.dial_color]
    elif selected and ref.get("selected_region_index") is not None:
        # Retain the selected region's color on short price/link follow-ups.
        selected_colors = re.findall(r"\b(?:" + COLORS + r")\b", fold_text(ref.get("catalog_query") or ""))
    # Search the color family; catalog titles often say "azul" rather than "azul claro".
    # Keep the precise shade in selected_option for visual comparison.
    from app.stories.story_selection import color_family
    selected_colors = list(dict.fromkeys(color_family(color) for color in selected_colors))
    updated["catalog_query_base"] = base.strip()
    # Size/color alone cannot safely identify a product family.
    meaningful = re.sub(SIZE, "", base).strip()
    updated["catalog_query"] = " ".join([base.strip(), *selected_colors]).strip() if meaningful else ""
    return updated
