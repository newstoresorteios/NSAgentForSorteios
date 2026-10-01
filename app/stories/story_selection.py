"""Customer selection of a visual region, never confirmation of a catalog SKU."""
from __future__ import annotations

import re
from app.catalog.retrieval.text import fold_text


def normalize_color(value: str | None) -> str:
    text = re.sub(r"[-_\u2010-\u2015]+", " ", fold_text(value or ""))
    text = " ".join(text.split())
    return {
        "blue": "azul", "light blue": "azul claro", "ice blue": "azul claro",
        "azul gelo": "azul claro", "black": "preto", "white": "branco",
        "silver": "prateado", "prata": "prateado", "green": "verde",
        "red": "vermelho", "orange": "laranja", "brown": "marrom",
        "gold": "dourado", "pink": "rosa", "grey": "cinza", "gray": "cinza",
    }.get(text, text)


def color_family(value: str | None) -> str:
    color = normalize_color(value).split(" ")[0]
    return "prata" if color == "prateado" else color


def selected_region(analysis, text):
    value = fold_text(text or "")
    if re.search(r"\b(?:nao|menos|exceto|outra|outro)\b", value):
        return None
    colors = re.findall(r"\b(?:azul(?:[ -]claro)?|preto|branco|prata|prateado|verde|rosa|cinza|dourado|marrom)\b", value)
    positions = re.findall(r"\b(?:centro|cima|baixo|esquerda|direita)\b", value)
    brands = {fold_text(r.brand_hypothesis) for r in analysis.product_regions if r.brand_hypothesis}
    named_brands = {b for b in brands if re.search(r'(?<!\w)' + re.escape(b) + r'(?!\w)', value)}
    if len(named_brands) > 1:
        return None
    if len(set(map(normalize_color, colors))) > 1 or len(set(positions)) > 1:
        return None
    model_matches = []
    for region in analysis.product_regions:
        hints = region.reference_hypothesis or ""
        hint_tokens = {
            token for token in re.findall(r"[a-z0-9]+", fold_text(hints))
            if len(token) >= 4 and token not in {"watch", "relogio", "automatic", "automatico"}
        }
        if hint_tokens and hint_tokens.intersection(set(re.findall(r"[a-z0-9]+", value))):
            model_matches.append(region)
    if len(model_matches) > 1:
        return None
    if not colors and not positions and not named_brands and not model_matches:
        return None
    position_map = {"center": "centro", "top": "cima", "bottom": "baixo", "left": "esquerda", "right": "direita"}
    matches = [r for r in analysis.product_regions
               if (not colors or (normalize_color(r.dial_color) == normalize_color(colors[0])
                   if " " in normalize_color(colors[0]) else color_family(r.dial_color) == color_family(colors[0])))
               and (not named_brands or fold_text(r.brand_hypothesis or '') in named_brands)
               and (not model_matches or r is model_matches[0])
               and (not positions or position_map.get(r.position) == positions[0])]
    return matches[0] if len(matches) == 1 else None


def selected_region_from_reference(ref, text):
    """Resolve a customer selector against the regions persisted for one Story."""
    from app.stories.instagram_story_models import StoryVisualUnderstanding, VisualProductRegion

    regions = []
    for raw in ref.get("story_regions") or []:
        try:
            regions.append(VisualProductRegion.model_validate(raw))
        except (TypeError, ValueError):
            continue
    if not regions:
        return None, None
    # A short color answer belongs to the brand already named by the customer,
    # not another watch of that color in the same scene.
    value = fold_text(text or '')
    brands = {fold_text(r.brand_hypothesis) for r in regions if r.brand_hypothesis}
    named = {b for b in brands if re.search(r'(?<!\w)' + re.escape(b) + r'(?!\w)', value)}
    prior = fold_text(ref.get('catalog_query_base') or '')
    prior_brands = {b for b in brands if re.search(r'(?<!\w)' + re.escape(b) + r'(?!\w)', prior)}
    scoped_regions = regions
    if not named and len(prior_brands) == 1:
        scoped_regions = [r for r in regions if fold_text(r.brand_hypothesis or '') in prior_brands]
    analysis = StoryVisualUnderstanding(
        watch_count=len(scoped_regions),
        multiple_products=len(scoped_regions) > 1,
        product_regions=scoped_regions,
    )
    region = selected_region(analysis, text)
    if region is None:
        return None, None
    return regions.index(region), region


def scope_visual_selection(analysis, text):
    region = selected_region(analysis, text)
    if region is None or len(analysis.product_regions) <= 1:
        return analysis, region
    return analysis_for_region(analysis, region), region


def analysis_for_region(analysis, region):
    """Isolate visible evidence without assigning whole-scene audio to one watch."""
    # Never combine the blue watch with a reference/brand read on its neighbour.
    scoped = analysis.model_copy(deep=True, update={
        "watch_count": 1, "multiple_products": False, "product_regions": [region],
        "dial_colors": [normalize_color(region.dial_color)] if region.dial_color else [],
        "strap_colors": [normalize_color(region.strap_color)] if region.strap_color else [],
        "visible_brands": [region.brand_hypothesis] if region.brand_hypothesis else [],
        "logo_hypotheses": [region.brand_hypothesis] if region.brand_hypothesis else [],
        "collection_hypotheses": [region.reference_hypothesis] if region.reference_hypothesis else [],
        "model_hypotheses": [region.reference_hypothesis] if region.reference_hypothesis else [],
        "visible_text": region.visible_text, "visible_references": [], "visible_skus": [], "visible_eans": [],
        # These scene-level attributes have no reliable per-watch assignment.
        "materials": [], "strap_types": [], "case_shapes": [], "visible_advertised_price": None,
        "mechanisms_suggested": region.mechanisms_suggested,
        "visual_description": region.label,
        "audio_transcript": "", "overlay_text": [],
    })
    return scoped


def reference_in_workspace(ref, runtime):
    if not runtime or not runtime.loaded or not runtime.enabled or not runtime.workspace_id or runtime.load_error:
        return False
    workspace = str(runtime.workspace_id)
    # Legacy UUID references remain supported. Aliases need an explicit, server-stamped workspace.
    if ref.get("workspace_id"):
        return (ref["workspace_id"] == workspace
                and ref.get("tenant_id") in {workspace, runtime.tenant_id})
    return ref.get("tenant_id") == workspace
