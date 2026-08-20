"""Match Story visual evidence to real catalog candidates (tenant-scoped)."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import Any

from .catalog_index import build_allowed_id_sets, reject_unknown_rerank_ids
from .config import get_settings
from .fact_authority import catalog_item_key_for
from .instagram_story_models import (
    StoryCandidateScore,
    StoryProductCandidate,
    StoryVisualUnderstanding,
)
from .observability import log_event
from .story_match_decider import rerank_candidates_with_consensus, try_resolve_tied_candidates


def _fold(value: Any) -> str:
    text = str(value or "").strip().casefold()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def classify_match(
    candidates: list[StoryProductCandidate],
    *,
    multiple_products: bool,
    analysis: StoryVisualUnderstanding | None = None,
) -> tuple[str, StoryProductCandidate | None]:
    settings = get_settings()
    exact_min = float(
        getattr(settings, "instagram_story_exact_match_min_confidence", None)
        or getattr(settings, "instagram_story_auto_match_min_confidence", 0.95)
        or 0.95
    )
    visual_min = float(
        getattr(settings, "instagram_story_visual_match_min_confidence", 0.96) or 0.96
    )
    amb_min = float(
        getattr(settings, "instagram_story_ambiguous_min_confidence", 0.65) or 0.65
    )
    margin = float(getattr(settings, "instagram_story_match_margin", 0.12) or 0.12)
    if not candidates:
        return "not_found", None
    ordered = sorted(candidates, key=lambda c: (-c.score, c.product_id))
    top1 = ordered[0]
    top2 = ordered[1] if len(ordered) > 1 else None
    gap = top1.score - (top2.score if top2 else 0.0)
    if analysis is not None and top2 is not None and gap < margin:
        resolved = try_resolve_tied_candidates(ordered, analysis, margin=margin)
        if resolved is not None:
            return "matched", resolved
    reasons = " ".join(top1.match_reasons).casefold()
    is_exact = any(
        token in reasons
        for token in ("ean:", "sku:", "reference:", "hash:", "tray_brand_model:", "store_url:")
    )
    if multiple_products:
        exactish = [
            cand
            for cand in ordered
            if any(
                token in " ".join(cand.match_reasons).casefold()
                for token in ("ean:", "sku:", "reference:", "tray_brand_model:", "store_url:")
            )
        ]
        unique_ids = {cand.product_id for cand in exactish[:4] if cand.product_id}
        if len(unique_ids) == 1 and exactish[0].score >= exact_min:
            return "matched", exactish[0]
        if top1.score >= amb_min:
            return "ambiguous", top1
        return "not_found", None
    if analysis is not None:
        from .story_match_decider import build_evidence_profile, score_catalog_overlap

        _, _, conflicts = score_catalog_overlap(top1, build_evidence_profile(analysis))
        if any(
            str(item).startswith("unseen_variant:") or str(item).startswith("unseen_material:")
            for item in conflicts
        ):
            if top2 is not None:
                resolved = try_resolve_tied_candidates(ordered, analysis, margin=0.0)
                if resolved is not None and resolved.product_id != top1.product_id:
                    return "matched", resolved
            return "ambiguous", top1
    threshold = exact_min if is_exact else visual_min
    top1_listing = _candidate_listing_key(top1)
    top2_listing = _candidate_listing_key(top2) if top2 else None
    top1_core = _candidate_core_listing_key(top1)
    top2_core = _candidate_core_listing_key(top2) if top2 else None
    if (
        top1_listing
        and top2_listing
        and top1_listing == top2_listing
        and top1.score >= min(threshold, exact_min)
        and top1.product_id
    ):
        return "matched", top1
    if (
        top1_core
        and top2_core
        and top1_core == top2_core
        and top1.score >= min(threshold, exact_min)
        and top1.product_id
        and is_exact
    ):
        return "matched", top1
    # Appearance-only must meet the stricter visual threshold.
    if top1.score >= threshold and gap >= margin and top1.product_id:
        # Never elevate confidence solely because there is a single result without evidence.
        if len(ordered) == 1 and not is_exact and top1.score < visual_min:
            if top1.score >= amb_min:
                return "ambiguous", top1
            return "not_found", None
        if top1.score_components and top1.score_components.conflicts:
            return "ambiguous", top1
        return "matched", top1
    if top1.score >= amb_min:
        return "ambiguous", top1
    return "not_found", None


def _candidate_listing_key(candidate: StoryProductCandidate) -> str | None:
    for reason in candidate.match_reasons or []:
        if str(reason).startswith("listing:"):
            return str(reason)
    return None


def _candidate_core_listing_key(candidate: StoryProductCandidate) -> str | None:
    """Normalize listing so bracelet SKU suffixes do not split the same watch."""
    raw = _candidate_listing_key(candidate)
    if not raw:
        return None
    text = raw[8:] if raw.startswith("listing:") else raw
    folded = _fold(text)
    # Drop trailing reference codes like c63-39ada3-s00v1-vc / hko / vk.
    folded = re.sub(r"\b[a-z]?\d{2,}[a-z0-9-]{4,}\b", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded or None


def _score_components(
    *,
    catalog_item_key: str,
    product_id: str,
    variant_id: str | None,
    exact: float = 0.0,
    visual: float = 0.0,
    lexical: float = 0.0,
    brand: float = 0.0,
    color: float = 0.0,
    model: float = 0.0,
    quality_penalty: float = 0.0,
    conflict_penalty: float = 0.0,
    reasons: list[str] | None = None,
    conflicts: list[str] | None = None,
    source: str = "catalog",
) -> StoryCandidateScore:
    final = max(
        0.0,
        min(
            1.0,
            exact
            + visual * 0.35
            + lexical * 0.15
            + brand * 0.1
            + color * 0.08
            + model * 0.1
            - quality_penalty
            - conflict_penalty,
        ),
    )
    # Exact identifier dominates.
    if exact >= 0.95:
        final = max(final, exact)
    return StoryCandidateScore(
        catalog_item_key=catalog_item_key,
        product_id=product_id,
        variant_id=variant_id,
        exact_identifier_score=exact,
        visual_similarity_score=visual,
        lexical_score=lexical,
        brand_score=brand,
        color_score=color,
        model_score=model,
        image_quality_penalty=quality_penalty,
        conflict_penalty=conflict_penalty,
        final_score=final,
        reasons=list(reasons or []),
        conflicts=list(conflicts or []),
        source=source,
    )


_COLOR_AND_SKIP = {
    "verde",
    "azul",
    "preto",
    "branco",
    "prata",
    "dourado",
    "rose",
    "rosa",
    "roxo",
    "lilas",
    "green",
    "blue",
    "black",
    "white",
    "silver",
    "gold",
    "purple",
    "violet",
    "laranja",
    "orange",
    "amarelo",
    "yellow",
    "marrom",
    "brown",
    "bege",
    "beige",
    "turquesa",
    "turquoise",
    "creme",
    "cream",
}

_GENERIC_AND_SKIP = {
    "main",
    "watch",
    "product",
    "relogio",
    "relogios",
    "modelo",
    "mostra",
    "mostrador",
    "pulseira",
    "caixa",
    "automatico",
    "mecanico",
    "quartz",
    "seminovo",
    "novo",
    "motor",
    "reserva",
    "energia",
    "swiss",
    "made",
    "confira",
    "limitada",
    "limitado",
    "unidades",
    "mundo",
    "apenas",
    "powermatic",
    "chronometer",
    "certified",
    "sapphire",
    "crystal",
}

# Common in many SKUs of the same brand. AND-searching these with a dial color
# returns Aachen/Basic first and color-locks before Leipzig/Tsuyosa variants.
_WEAK_MODEL_TOKENS = {
    "pilot",
    "sport",
    "navy",
    "basic",
    "classic",
    "automatic",
    "mecanico",
    "quartz",
}

_COLOR_SYNONYMS: dict[str, tuple[str, ...]] = {
    "purple": ("roxo", "purple", "violet"),
    "violet": ("roxo", "purple", "violet"),
    "roxo": ("roxo", "purple", "violet"),
    "green": ("verde", "green"),
    "verde": ("verde", "green"),
    "blue": ("azul", "blue"),
    "azul": ("azul", "blue"),
    "light blue": ("azul", "gelo", "blue"),
    "azul claro": ("azul", "gelo", "blue"),
    "azul gelo": ("azul", "gelo", "blue"),
    "ice blue": ("azul", "gelo", "blue"),
    "gelo": ("azul", "gelo", "blue"),
    "black": ("preto", "black"),
    "preto": ("preto", "black"),
    "orange": ("laranja", "orange"),
    "laranja": ("laranja", "orange"),
    "yellow": ("amarelo", "yellow"),
    "amarelo": ("amarelo", "yellow"),
    "pink": ("rosa", "pink"),
    "rosa": ("rosa", "pink"),
    "white": ("branco", "white"),
    "branco": ("branco", "white"),
    "silver": ("prata", "silver"),
    "prata": ("prata", "silver"),
    "gold": ("dourado", "gold"),
    "dourado": ("dourado", "gold"),
    "brown": ("marrom", "brown"),
    "marrom": ("marrom", "brown"),
    "beige": ("bege", "beige"),
    "bege": ("bege", "beige"),
    "turquoise": ("turquesa", "turquoise"),
    "turquesa": ("turquesa", "turquoise"),
    "cream": ("creme", "cream"),
    "creme": ("creme", "cream"),
}

# Primary Portuguese color used in Tray AND jobs (never multi-word).
_COLOR_SEARCH_PRIMARY: dict[str, str] = {
    "purple": "roxo",
    "violet": "roxo",
    "roxo": "roxo",
    "green": "verde",
    "verde": "verde",
    "blue": "azul",
    "azul": "azul",
    "light blue": "azul",
    "azul claro": "azul",
    "azul gelo": "azul",
    "ice blue": "azul",
    "gelo": "azul",
    "black": "preto",
    "preto": "preto",
    "orange": "laranja",
    "laranja": "laranja",
    "yellow": "amarelo",
    "amarelo": "amarelo",
    "pink": "rosa",
    "rosa": "rosa",
    "white": "branco",
    "branco": "branco",
}


def _is_calendar_year_token(token: str) -> bool:
    """Founding years on dials (Bulova 1875) are not catalog references."""
    raw = str(token or "").strip()
    if not raw.isdigit() or len(raw) != 4:
        return False
    year = int(raw)
    return 1800 <= year <= 2100


def _model_tokens_for_match(tokens: list[str], brand: str | None) -> list[str]:
    brand_fold = _fold(brand)
    out: list[str] = []
    for token in tokens:
        key = _fold(token)
        if len(token) < 3:
            continue
        if key in _COLOR_AND_SKIP or key in _GENERIC_AND_SKIP or key in _WEAK_MODEL_TOKENS:
            continue
        if brand_fold and key == brand_fold:
            continue
        if _is_calendar_year_token(token):
            continue
        out.append(token)
    return out


def _is_sku_like(token: str) -> bool:
    raw = str(token or "").strip()
    if " " in raw or ":" in raw:
        return False
    compact = raw.replace("-", "").replace(".", "")
    if len(compact) < 5 or len(compact) > 16:
        return False
    has_letter = any(ch.isalpha() for ch in compact)
    has_digit = any(ch.isdigit() for ch in compact)
    return has_letter and has_digit


def _dial_color_score(analysis: StoryVisualUnderstanding, blob: str) -> float:
    wanted: set[str] = set()
    for raw in analysis.dial_colors or []:
        wanted.update(_COLOR_SYNONYMS.get(_fold(raw), (_fold(raw),)))
    for region in analysis.product_regions or []:
        dial = _fold(getattr(region, "dial_color", None))
        if dial:
            wanted.update(_COLOR_SYNONYMS.get(dial, (dial,)))
    if not wanted:
        return 0.0
    if any(token in blob for token in wanted if token):
        return 0.9
    return 0.0


def tray_search_plan(analysis: StoryVisualUnderstanding) -> tuple[str | None, list[str]]:
    """Brand + distinctive model tokens for TRAYadaptor AND search (no stock filter)."""
    brand: str | None = None
    for raw in (*(analysis.visible_brands or []), *(analysis.logo_hypotheses or [])):
        text = str(raw or "").strip()
        if text:
            brand = text
            break
    tokens: list[str] = []
    seen: set[str] = set()
    brand_fold = _fold(brand)

    def _add_phrase(raw: Any, *, allow_generic: bool = False) -> None:
        for part in str(raw or "").replace("/", " ").replace("-", " ").split():
            token = part.strip().strip(":.,;")
            if len(token) < 3:
                continue
            if token.isdigit() and len(token) < 5:
                continue
            if _is_calendar_year_token(token):
                continue
            key = _fold(token)
            if not key or key in seen or key == brand_fold or key in _COLOR_AND_SKIP:
                continue
            if key.endswith("mm") and key[:-2].isdigit():
                continue
            if not allow_generic and key in _GENERIC_AND_SKIP:
                continue
            seen.add(key)
            tokens.append(token)

    for sku in analysis.visible_skus or []:
        _add_phrase(sku)
    for ref in analysis.visible_references or []:
        _add_phrase(ref)
    brand_names = [str(b) for b in (analysis.visible_brands or [])]
    for text in analysis.visible_text or []:
        raw = str(text or "").strip()
        if _is_sku_like(raw):
            _add_phrase(raw)
            continue
        from .story_match_decider import _is_brand_only_line

        if brand_names and _is_brand_only_line(raw, brand_names):
            continue
        folded_line = _fold(raw)
        if any(
            marker in folded_line
            for marker in ("limitado", "unidades no mundo", "seminovo e", "confira")
        ) and not any(_is_sku_like(part) for part in raw.split()):
            continue
        word_count = len([p for p in raw.replace("/", " ").split() if len(p.strip()) >= 2])
        if len(raw) >= 12 and word_count >= 2:
            _add_phrase(raw)
    for model in analysis.model_hypotheses or []:
        _add_phrase(model)
    for collection in analysis.collection_hypotheses or []:
        _add_phrase(collection)
    for region in analysis.product_regions or []:
        _add_phrase(getattr(region, "reference_hypothesis", None))
    return brand, tokens[:3]


def distinctive_search_tokens(tokens: list[str]) -> list[str]:
    """Prefer collection names (Leipzig, Tsuyosa) over shared line words (Pilot)."""
    strong = [
        token
        for token in tokens
        if _fold(token) not in _WEAK_MODEL_TOKENS
        and _fold(token) not in _COLOR_AND_SKIP
        and _fold(token) not in _GENERIC_AND_SKIP
        and not _is_calendar_year_token(token)
    ]
    return strong or list(tokens)


def collection_head_token(
    analysis: StoryVisualUnderstanding,
    tokens: list[str],
) -> str | None:
    """Last distinctive word of the model line: Summer, Leipzig — not Hermétique/Pilot."""
    phrases = [
        *(analysis.model_hypotheses or []),
        *(analysis.collection_hypotheses or []),
    ]
    for phrase in phrases:
        parts = [
            part.strip().strip(":.,;")
            for part in str(phrase or "").replace("/", " ").replace("-", " ").split()
        ]
        strong = [
            token
            for token in distinctive_search_tokens(parts)
            if not token.isdigit()
            and not _fold(token).endswith("mm")
            and _fold(token) not in {"powermatic"}
        ]
        if strong:
            named = [token for token in strong if len(_fold(token)) >= 6]
            return named[-1] if named else strong[-1]
    distinctive = distinctive_search_tokens(tokens)
    if distinctive:
        return distinctive[-1]
    return tokens[0] if tokens else None


def tokens_from_store_url(url: str | None) -> tuple[str | None, list[str]]:
    from urllib.parse import urlparse

    raw = str(url or "").strip()
    if not raw:
        return None, []
    path = urlparse(raw).path.replace("_", "-")
    slug = path.rstrip("/").split("/")[-1]
    skip = {
        "relogio",
        "relogios",
        "seminovo",
        "automatico",
        "mecanico",
        "quartz",
        "www",
        "http",
        "https",
    }
    tokens: list[str] = []
    brand: str | None = None
    for part in slug.replace("-", " ").split():
        key = _fold(part)
        if len(key) < 3 or key in skip or key in _COLOR_AND_SKIP:
            continue
        if brand is None and key in {
            "mido",
            "laco",
            "bulova",
            "seiko",
            "tissot",
            "orient",
            "casio",
            "citizen",
        }:
            brand = part
            continue
        tokens.append(part)
    return brand, tokens[:8]


def _dial_color_search_tokens(analysis: StoryVisualUnderstanding) -> list[str]:
    """Single Portuguese color words for Tray AND (never 'azul'+'claro')."""
    ordered: list[str] = []
    seen: set[str] = set()
    raws = list(analysis.dial_colors or [])
    for region in analysis.product_regions or []:
        dial = str(getattr(region, "dial_color", None) or "").strip()
        if dial:
            raws.append(dial)
    for raw in raws:
        folded = _fold(raw)
        primary = _COLOR_SEARCH_PRIMARY.get(folded)
        if primary is None and " " in folded:
            primary = _COLOR_SEARCH_PRIMARY.get(folded.split()[-1]) or _COLOR_SEARCH_PRIMARY.get(
                folded.split()[0]
            )
        if primary is None:
            # Keep only a single alphabetic word — never split ice-blue into two ANDs.
            parts = [part for part in folded.replace("-", " ").split() if part.isalpha()]
            primary = parts[0] if len(parts) == 1 else None
        if not primary or primary in seen:
            continue
        seen.add(primary)
        ordered.append(primary)
    return ordered[:2]


def tray_search_jobs(
    analysis: StoryVisualUnderstanding,
    *,
    store_url: str | None = None,
) -> list[tuple[str | None, list[str]]]:
    jobs: list[tuple[str | None, list[str]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def _add(brand: str | None, tokens: list[str]) -> None:
        cleaned = [token for token in tokens if str(token or "").strip()]
        if not cleaned:
            return
        key = (_fold(brand), tuple(_fold(t) for t in cleaned))
        if key in seen or (not brand and not cleaned):
            return
        seen.add(key)
        jobs.append((brand, cleaned))

    from .story_match_decider import model_line_search_tokens

    slug_brand, slug_tokens = tokens_from_store_url(store_url)
    brand, tokens = tray_search_plan(analysis)
    model_line = _model_tokens_for_match(
        model_line_search_tokens(analysis),
        brand,
    )
    distinctive = distinctive_search_tokens(tokens)
    head = collection_head_token(analysis, tokens)
    colors = _dial_color_search_tokens(analysis)
    model_line_has_ref = any(
        re.search(r"[a-z]\d", _fold(token), re.IGNORECASE) for token in model_line
    )
    prefer_model_line_first = len(model_line) >= 3 or model_line_has_ref

    # Most specific first — one good query beats many paginated broad scans.
    if brand and prefer_model_line_first and len(model_line) >= 2:
        _add(brand, model_line)
        if len(model_line) >= 3:
            _add(brand, model_line[:2])
        # Drop rare marketing suffixes (Rocks) that empty AND matches.
        core = [t for t in model_line if _fold(t) not in {"rocks", "mk2", "mk"}]
        if len(core) >= 2 and core != model_line[:2]:
            _add(brand, core[:2])
    elif brand and head and colors:
        for color in colors[:1]:
            _add(brand, [head, color])
    if brand and not prefer_model_line_first and len(model_line) >= 2:
        _add(brand, model_line)
        if len(model_line) >= 3:
            _add(brand, model_line[:2])
    elif brand and head and colors and prefer_model_line_first:
        for color in colors[:1]:
            _add(brand, [head, color])
    if brand and head:
        _add(brand, [head])
        if colors:
            for color in colors[:1]:
                _add(brand, [head, color])
    # Family + color without rare suffix (C63 Verde finds Seander typo listings).
    if brand and colors:
        family = next(
            (
                t
                for t in (*model_line, *distinctive, *([head] if head else []))
                if re.search(r"[a-z]\d", _fold(t), re.IGNORECASE)
                or (len(_fold(t)) >= 6 and _fold(t) not in _COLOR_AND_SKIP)
            ),
            None,
        )
        if family:
            _add(brand, [family, colors[0]])
            _add(brand, [family])
    if brand and distinctive and distinctive != model_line:
        _add(brand, distinctive[:3])
    _add(slug_brand, slug_tokens)
    return jobs[:6]


def _title_lines_for_search(analysis: StoryVisualUnderstanding) -> list[str]:
    from .story_match_decider import _title_like_lines

    return _title_like_lines(analysis)


_STORY_TRAY_PAGE_SIZE = 50
_STORY_TRAY_MAX_PAGES = 2
_STORY_TRAY_CONCURRENCY = 4

_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "sealander": ("sealander", "seander"),
    "seander": ("sealander", "seander"),
}


def _token_in_text(blob: str, token: str) -> bool:
    key = _fold(token)
    if not key:
        return False
    if key in blob:
        return True
    return any(alias in blob for alias in _TOKEN_ALIASES.get(key, ()))


def _product_text_blob(product: dict[str, Any]) -> str:
    return _fold(
        " ".join(
            str(product.get(field) or "")
            for field in (
                "name",
                "title",
                "url",
                "link",
                "product_url",
                "reference",
                "model",
                "description",
                "brand",
            )
        )
    )


def _tray_search_page_done(
    products: list[Any],
    paging: dict[str, Any] | None,
    *,
    page: int,
    page_size: int,
) -> bool:
    if not products:
        return True
    if paging and paging.get("total") is not None:
        try:
            total = int(paging["total"])
            limit = int(paging.get("limit") or page_size)
        except (TypeError, ValueError):
            return False
        return page * limit >= total
    if len(products) < page_size:
        return True
    return False


def _candidate_has_dial_color_lock(candidate: StoryProductCandidate) -> bool:
    components = candidate.score_components
    if components is not None and components.color_score >= 0.8 and candidate.score >= 0.9:
        return True
    reasons = " ".join(candidate.match_reasons).casefold()
    return "tray_brand_model:" in reasons and candidate.score >= 0.9


async def match_story_to_catalog(
    *,
    tenant_id: str,
    analysis: StoryVisualUnderstanding,
    execute_tool: Any | None = None,
    media_bytes: bytes | None = None,
    store_url: str | None = None,
) -> list[StoryProductCandidate]:
    """Build candidates from exact identifiers → visual index → lexical → Tray.

    Never invents product IDs. Scores reflect real evidence components.
    """
    if not str(tenant_id or "").strip():
        raise ValueError("tenant_id required")
    _ = media_bytes
    settings = get_settings()
    limit = int(getattr(settings, "instagram_story_max_candidates", 10) or 10)
    candidates: list[StoryProductCandidate] = []
    seen: set[str] = set()
    quality_penalty = 0.15 if analysis.image_quality == "poor" else 0.0

    def _add_scored(product: dict[str, Any], components: StoryCandidateScore) -> None:
        if not isinstance(product, dict) or product.get("id") is None:
            return
        # Tenant isolation — reject foreign rows if present.
        product_tenant = str(product.get("tenant_id") or tenant_id).strip()
        if product_tenant and product_tenant != tenant_id:
            return
        key = components.catalog_item_key
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            StoryProductCandidate(
                catalog_item_key=key,
                product_id=components.product_id,
                variant_id=components.variant_id,
                score=components.final_score,
                match_reasons=list(components.reasons),
                mismatch_reasons=list(components.conflicts),
                source=components.source,
                score_components=components,
            )
        )

    # Level 1 — exact identifiers via catalog index.
    try:
        from .catalog_index_repository import CatalogIndexRepository, row_to_product_dict

        repo = CatalogIndexRepository()
        for ean in analysis.visible_eans:
            for row in repo.search_exact(tenant_id=tenant_id, ean=str(ean)):
                product = row_to_product_dict(row)
                pid = str(product["id"])
                vid = str(product["variant_id"]) if product.get("variant_id") else None
                _add_scored(
                    product,
                    _score_components(
                        catalog_item_key=catalog_item_key_for(pid, vid),
                        product_id=pid,
                        variant_id=vid,
                        exact=0.99,
                        quality_penalty=quality_penalty,
                        reasons=[f"ean:{ean}"],
                        source="exact_ean",
                    ),
                )
        for sku in analysis.visible_skus:
            for row in repo.search_exact(tenant_id=tenant_id, sku=str(sku)):
                product = row_to_product_dict(row)
                pid = str(product["id"])
                vid = str(product["variant_id"]) if product.get("variant_id") else None
                _add_scored(
                    product,
                    _score_components(
                        catalog_item_key=catalog_item_key_for(pid, vid),
                        product_id=pid,
                        variant_id=vid,
                        exact=0.98,
                        quality_penalty=quality_penalty,
                        reasons=[f"sku:{sku}"],
                        source="exact_sku",
                    ),
                )
        for ref in analysis.visible_references:
            for row in repo.search_exact(tenant_id=tenant_id, reference=str(ref)):
                product = row_to_product_dict(row)
                pid = str(product["id"])
                vid = str(product["variant_id"]) if product.get("variant_id") else None
                _add_scored(
                    product,
                    _score_components(
                        catalog_item_key=catalog_item_key_for(pid, vid),
                        product_id=pid,
                        variant_id=vid,
                        exact=0.97,
                        quality_penalty=quality_penalty,
                        reasons=[f"reference:{ref}"],
                        source="exact_reference",
                    ),
                )
            for brand in analysis.visible_brands or analysis.logo_hypotheses:
                rows = repo.search_lexical(
                    tenant_id=tenant_id,
                    query=f"{brand} {ref}".strip(),
                    brand=brand,
                )
                for row in rows[:5]:
                    product = row_to_product_dict(row)
                    pid = str(product["id"])
                    vid = str(product["variant_id"]) if product.get("variant_id") else None
                    conflicts: list[str] = []
                    brand_score = 0.7
                    row_brand = _fold(product.get("brand"))
                    if row_brand and _fold(brand) and row_brand != _fold(brand):
                        conflicts.append("brand_conflict")
                        brand_score = 0.0
                    _add_scored(
                        product,
                        _score_components(
                            catalog_item_key=catalog_item_key_for(pid, vid),
                            product_id=pid,
                            variant_id=vid,
                            lexical=0.55,
                            brand=brand_score,
                            model=0.4,
                            quality_penalty=quality_penalty,
                            conflict_penalty=0.25 if conflicts else 0.0,
                            reasons=[f"brand_ref:{brand}:{ref}"],
                            conflicts=conflicts,
                            source="lexical",
                        ),
                    )
        for brand in analysis.visible_brands or analysis.logo_hypotheses:
            for model in (analysis.model_hypotheses or [])[:2]:
                query = f"{brand} {model}".strip()
                if len(query) < 4:
                    continue
                rows = repo.search_lexical(
                    tenant_id=tenant_id,
                    query=model.strip() or brand,
                    brand=brand,
                )
                for row in rows[:5]:
                    product = row_to_product_dict(row)
                    pid = str(product["id"])
                    vid = str(product["variant_id"]) if product.get("variant_id") else None
                    name_l = _fold(product.get("name") or product.get("title"))
                    model_hits = sum(
                        1
                        for part in str(model).replace("-", " ").split()
                        if len(part) >= 3 and _fold(part) in name_l
                    )
                    if model_hits < 1:
                        continue
                    _add_scored(
                        product,
                        _score_components(
                            catalog_item_key=catalog_item_key_for(pid, vid),
                            product_id=pid,
                            variant_id=vid,
                            lexical=0.6,
                            brand=0.8,
                            model=min(0.8, 0.3 * model_hits),
                            quality_penalty=quality_penalty,
                            reasons=[f"brand_model:{query[:60]}"],
                            source="lexical",
                        ),
                    )
    except Exception as exc:  # noqa: BLE001
        print("[story.matcher.index.error]", {"error_type": type(exc).__name__})

    # Level 2+3 — visual neighbors and live Tray run concurrently (I/O bound).
    has_exact = any(str(c.source or "").startswith("exact_") for c in candidates)
    color_required = bool(analysis.dial_colors)
    color_locked = any(_candidate_has_dial_color_lock(c) for c in candidates)
    page_size = _STORY_TRAY_PAGE_SIZE
    _, planned_tokens = tray_search_plan(analysis)
    analysis_distinctive = [
        token
        for token in distinctive_search_tokens(planned_tokens)
        if _fold(token) not in _COLOR_AND_SKIP
    ]
    evidence_blob = _fold(
        " ".join(
            [
                *(analysis.visible_text or []),
                *(analysis.visible_brands or []),
                *(analysis.model_hypotheses or []),
                *(analysis.collection_hypotheses or []),
                *(analysis.visible_skus or []),
                *(analysis.visible_references or []),
            ]
        )
    )
    slug = _fold((store_url or "").rstrip("/").split("/")[-1])

    async def _load_visual_neighbors() -> list[dict[str, Any]]:
        try:
            from .product_image_index import visual_search_from_caption

            caption_bits = [
                *analysis.visible_brands[:2],
                *analysis.model_hypotheses[:2],
                *analysis.visible_references[:1],
                *analysis.dial_colors[:1],
                *analysis.strap_colors[:1],
                analysis.visual_description[:180],
            ]
            caption = " ".join(str(x) for x in caption_bits if x).strip()
            if not caption:
                return []
            return list(await visual_search_from_caption(caption) or [])[:limit]
        except Exception as exc:  # noqa: BLE001
            print("[story.matcher.visual.error]", {"error_type": type(exc).__name__})
            return []

    def _apply_visual_neighbors(neighbors: list[dict[str, Any]]) -> None:
        for neighbor in neighbors:
            pid = str(neighbor.get("product_id") or "").strip()
            if not pid:
                continue
            distance = float(neighbor.get("distance") or 1.0)
            visual = max(0.0, min(1.0, 1.0 - distance))
            conflicts: list[str] = []
            brand_score = 0.0
            nb_brand = _fold(neighbor.get("brand"))
            if analysis.visible_brands:
                if nb_brand and nb_brand in {_fold(b) for b in analysis.visible_brands}:
                    brand_score = 0.8
                elif nb_brand:
                    conflicts.append("brand_conflict")
            color_score = 0.0
            if analysis.dial_colors:
                caption_l = _fold(neighbor.get("visual_caption") or neighbor.get("name"))
                if any(_fold(c) in caption_l for c in analysis.dial_colors):
                    color_score = 0.5
            _add_scored(
                {
                    "id": pid,
                    "variant_id": neighbor.get("variant_id"),
                    "brand": neighbor.get("brand"),
                    "name": neighbor.get("name"),
                    "tenant_id": tenant_id,
                },
                _score_components(
                    catalog_item_key=catalog_item_key_for(
                        pid,
                        str(neighbor["variant_id"])
                        if neighbor.get("variant_id") is not None
                        else None,
                    ),
                    product_id=pid,
                    variant_id=(
                        str(neighbor["variant_id"])
                        if neighbor.get("variant_id") is not None
                        else None
                    ),
                    visual=visual,
                    brand=brand_score,
                    color=color_score,
                    quality_penalty=quality_penalty,
                    conflict_penalty=0.3 if conflicts else 0.0,
                    reasons=[f"visual_neighbor:d={distance:.3f}"],
                    conflicts=conflicts,
                    source="visual_index",
                ),
            )

    def _ingest_tray_page(
        *,
        brand: str | None,
        tokens: list[str],
        page: int,
        products: list[Any],
        paging: dict[str, Any] | None,
    ) -> bool:
        nonlocal color_locked
        if not products:
            log_event(
                "story_tray_search_exhausted",
                {
                    "brand": brand,
                    "token_count": len(tokens),
                    "page": page,
                    "pages_scanned": 0,
                    "reason": "empty_page",
                },
            )
            return color_locked
        try:
            from .catalog_index import index_products_best_effort

            index_products_best_effort(products, factual_source="tray_search")
        except Exception:
            pass
        for idx, product in enumerate(products):
            if not isinstance(product, dict):
                continue
            pid = str(product.get("id") or "")
            if not pid:
                continue
            vid = (
                str(product["variant_id"])
                if product.get("variant_id") is not None
                else None
            )
            name_l = _fold(product.get("name") or product.get("title"))
            blob = _product_text_blob(product)
            product_brand = _fold(product.get("brand"))
            brand_ok = bool(brand) and (
                _fold(brand) in name_l
                or _fold(brand) in blob
                or product_brand == _fold(brand)
            )
            distinctive = tokens or [
                part for part in (brand or "").split() if len(part) >= 3
            ]
            hits = sum(
                1 for token in distinctive if len(token) >= 3 and _token_in_text(blob, token)
            )
            url_hit = bool(slug) and len(slug) >= 8 and slug in blob
            color = _dial_color_score(analysis, blob)
            material_conflicts = [
                f"unseen_material:{marker}"
                for marker in ("bronze", "titane", "titanium", "gmt", "chrono", "cronografo")
                if marker in blob and marker not in evidence_blob
            ]
            model_tokens = _model_tokens_for_match(tokens, brand)
            model_hits = sum(
                1 for token in model_tokens if len(token) >= 3 and _token_in_text(blob, token)
            )
            if hits < 1 and not url_hit:
                continue
            strong = url_hit or (
                brand_ok
                and bool(model_tokens)
                and model_hits >= min(2, max(len(model_tokens), 1))
                and (not color_required or color >= 0.8)
            )
            if color_required and color < 0.8 and not url_hit:
                strong = False
            rank_penalty = ((page - 1) * page_size + idx) * 0.002
            name_key = _fold(product.get("name") or product.get("title") or "")
            reason = (
                f"store_url:{slug[:80]}"
                if url_hit
                else (
                    f"tray_brand_model:{brand or ''} {' '.join(tokens)}".strip()
                    if strong
                    else f"tray_query_overlap:{' '.join(tokens)[:40]}"
                )
            )
            reasons = [reason]
            if name_key:
                reasons.append(f"listing:{name_key[:80]}")
            _add_scored(
                {**product, "tenant_id": product.get("tenant_id") or tenant_id},
                _score_components(
                    catalog_item_key=catalog_item_key_for(pid, vid),
                    product_id=pid,
                    variant_id=vid,
                    exact=(
                        0.96
                        if url_hit
                        else (0.7 if material_conflicts else (0.92 if strong else 0.0))
                    ),
                    lexical=min(0.6, 0.2 * max(hits, 1) - rank_penalty),
                    brand=0.85 if brand_ok else 0.2,
                    model=min(0.8, 0.25 * hits),
                    color=color,
                    quality_penalty=quality_penalty,
                    conflict_penalty=0.35 if material_conflicts else 0.0,
                    reasons=reasons,
                    conflicts=material_conflicts,
                    source="tray_search",
                ),
            )
            if color_required and color >= 0.8 and strong and analysis_distinctive:
                distinctive_hit = all(
                    _token_in_text(blob, token)
                    for token in analysis_distinctive
                    if len(token) >= 3
                )
                if distinctive_hit:
                    color_locked = True
        log_event(
            "story_tray_search_page",
            {
                "brand": brand,
                "token_count": len(tokens),
                "page": page,
                "page_count": len(products),
                "color_locked": color_locked,
                "paging_total": paging.get("total") if paging else None,
            },
        )
        if _tray_search_page_done(products, paging, page=page, page_size=page_size):
            log_event(
                "story_tray_search_exhausted",
                {
                    "brand": brand,
                    "token_count": len(tokens),
                    "page": page,
                    "pages_scanned": page,
                    "reason": "last_page",
                    "color_locked": color_locked,
                },
            )
        return color_locked

    async def _fetch_tray_page(
        brand: str | None,
        tokens: list[str],
        page: int,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"limit": page_size, "page": page}
        if tokens:
            arguments["tokens"] = tokens
        if brand:
            arguments["brand"] = brand
        result = await execute_tool("search_products", arguments)
        products = result.get("products") if isinstance(result, dict) else None
        paging = result.get("paging") if isinstance(result, dict) else None
        return {
            "brand": brand,
            "tokens": tokens,
            "page": page,
            "products": products if isinstance(products, list) else [],
            "paging": paging if isinstance(paging, dict) else None,
        }

    async def _gather_tray_pages(
        items: list[tuple[str | None, list[str], int]],
    ) -> list[dict[str, Any]]:
        if not items:
            return []
        sem = asyncio.Semaphore(_STORY_TRAY_CONCURRENCY)

        async def _one(brand: str | None, tokens: list[str], page: int) -> dict[str, Any]:
            async with sem:
                try:
                    return await _fetch_tray_page(brand, tokens, page)
                except Exception as exc:  # noqa: BLE001
                    print("[story.matcher.tray.error]", {"error_type": type(exc).__name__})
                    return {
                        "brand": brand,
                        "tokens": tokens,
                        "page": page,
                        "products": [],
                        "paging": None,
                    }

        return list(await asyncio.gather(*[_one(b, t, p) for b, t, p in items]))

    async def _load_tray_pages() -> list[dict[str, Any]]:
        if execute_tool is None or has_exact:
            return []
        jobs = tray_search_jobs(analysis, store_url=store_url)
        if not jobs:
            return []
        first = await _gather_tray_pages([(brand, tokens, 1) for brand, tokens in jobs])
        follow_up: list[tuple[str | None, list[str], int]] = []
        strong_model_hit = False
        for brand, tokens in jobs:
            row = next(
                (
                    item
                    for item in first
                    if item.get("brand") == brand and item.get("tokens") == tokens
                ),
                None,
            )
            if row is None:
                continue
            products = row.get("products") or []
            model_token_count = len(_model_tokens_for_match(tokens, brand))
            if products and model_token_count >= 2:
                strong_model_hit = True
            max_pages = 1 if model_token_count >= 2 else _STORY_TRAY_MAX_PAGES
            if max_pages < 2 or not products:
                continue
            if _tray_search_page_done(
                products,
                row.get("paging"),
                page=1,
                page_size=page_size,
            ):
                continue
            follow_up.append((brand, tokens, 2))
        extra: list[dict[str, Any]] = []
        if follow_up and not strong_model_hit:
            extra = await _gather_tray_pages(follow_up)
        return [*first, *extra]

    visual_neighbors, tray_pages = await asyncio.gather(
        _load_visual_neighbors(),
        _load_tray_pages(),
    )
    _apply_visual_neighbors(visual_neighbors)
    color_locked = color_locked or any(
        _candidate_has_dial_color_lock(c) for c in candidates
    )
    for row in tray_pages:
        if color_locked:
            break
        _ingest_tray_page(
            brand=row.get("brand"),
            tokens=list(row.get("tokens") or []),
            page=int(row.get("page") or 1),
            products=list(row.get("products") or []),
            paging=row.get("paging"),
        )

    ordered = sorted(candidates, key=lambda c: (-c.score, c.product_id))[:limit]
    if analysis is not None and ordered:
        ordered = rerank_candidates_with_consensus(ordered, analysis)[:limit]
    log_event(
        "instagram_story.candidates_found",
        {
            "count": len(ordered),
            "top_score": ordered[0].score if ordered else None,
            "multiple_products": analysis.multiple_products,
            "tenant_id": tenant_id,
        },
    )
    return ordered


def reject_invented_rerank_ids(
    selected_ids: list[str],
    candidates: list[StoryProductCandidate],
) -> tuple[list[str], int]:
    allowed = {c.product_id for c in candidates}
    return reject_unknown_rerank_ids(
        selected_ids,
        allowed,
        limit=max(len(selected_ids), len(allowed), 1),
    )


def candidates_to_allowed_sets(
    candidates: list[StoryProductCandidate],
) -> dict[str, set[str]]:
    products = [
        {
            "id": c.product_id,
            "variant_id": c.variant_id,
            "catalog_item_key": c.catalog_item_key,
        }
        for c in candidates
    ]
    return build_allowed_id_sets(products)
