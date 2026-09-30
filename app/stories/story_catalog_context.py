"""Bounded visual search hints, never an assertion of product identity."""
import re

from app.catalog.retrieval.text import fold_text


COLORS = r"azul(?: claro)?|preto|prateado|prata|branco|verde|marrom|dourado|rosa|vermelho|laranja"
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
    value = fold_text(text or "")
    base = str(ref.get("catalog_query_base") or "")[:200]
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
    if selectors:
        normalize = lambda v: re.sub(r"\bprata\b", "prateado", fold_text(v))
        matches = [v for v in options if all(normalize(s) in normalize(v) for s in selectors)]
        updated["selected_option"] = matches[0] if len(matches) == 1 else None
    selected = updated.get("selected_option")
    selected_colors = re.findall(r"\b(?:" + COLORS + r")\b", fold_text(selected or ""))
    updated["catalog_query_base"] = base.strip()
    # Size/color alone cannot safely identify a product family.
    meaningful = re.sub(SIZE, "", base).strip()
    updated["catalog_query"] = " ".join([base.strip(), *selected_colors]).strip() if meaningful else ""
    return updated
