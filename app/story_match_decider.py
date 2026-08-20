"""Three-path Story evidence evaluators + consensus decider for catalog matching.

Paths (same photo, no per-watch rules):
1. Text — OCR lines, references, SKUs on the Story art
2. Structure — brand, dial/strap colors, case shape, mechanism from vision
3. Catalog overlap — how well each Tray listing matches paths 1+2

When Tray returns tied scores (e.g. two green C63 variants), the decider breaks
the tie using visible text such as "Sealander Rocks" and penalizes absent
variants (GMT, bronze) not mentioned in the Story.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .instagram_story_models import StoryProductCandidate, StoryVisualUnderstanding
from .observability import log_event


def _fold(value: Any) -> str:
    text = str(value or "").strip().casefold()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


_SIZE_RE = re.compile(r"\b(\d{2})\s*mm\b", re.IGNORECASE)

_GENERIC_TITLE_SKIP = {
    "main",
    "watch",
    "product",
    "relogio",
    "relogios",
    "modelo",
    "automatico",
    "mecanico",
    "quartz",
    "motor",
    "swiss",
    "made",
    "confira",
    "reserva",
    "energia",
    "powermatic",
    "seminovo",
    "limitado",
    "limitada",
    "unidades",
    "mundo",
    "apenas",
    "chronometer",
    "sapphire",
    "crystal",
    "stainless",
    "steel",
}

_WEAK_TITLE_TOKENS = {
    "pilot",
    "sport",
    "navy",
    "basic",
    "classic",
    "automatic",
    "mecanico",
    "quartz",
}

# Listing words that distinguish variants; penalize when absent from Story evidence.
_VARIANT_MARKERS = (
    "gmt",
    "bronze",
    "titane",
    "titanium",
    "elite",
    "extreme",
    "chrono",
    "cronografo",
)

_COLOR_SYNONYMS: dict[str, tuple[str, ...]] = {
    "green": ("verde", "green"),
    "verde": ("verde", "green"),
    "black": ("preto", "black"),
    "preto": ("preto", "black"),
    "blue": ("azul", "blue"),
    "azul": ("azul", "blue"),
    "white": ("branco", "white"),
    "branco": ("branco", "white"),
    "orange": ("laranja", "orange"),
    "laranja": ("laranja", "orange"),
}


@dataclass
class StoryEvidenceProfile:
    """Merged evidence from text + structure paths."""

    positive_tokens: set[str] = field(default_factory=set)
    model_lines: list[str] = field(default_factory=list)
    size_mm: str | None = None
    colors: set[str] = field(default_factory=set)
    text_path_score: float = 0.0
    structure_path_score: float = 0.0


def _tokenize_phrase(raw: Any) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").replace("/", " ").replace("-", " ").split():
        token = part.strip().strip(":.,;")
        if len(token) >= 2:
            out.append(token)
    return out


def _brand_token_set(brands: list[str]) -> set[str]:
    parts: set[str] = set()
    for brand in brands:
        for token in _tokenize_phrase(brand):
            parts.add(_fold(token))
    return parts


def _is_brand_only_line(text: str, brands: list[str]) -> bool:
    """Skip 'CHRISTOPHER WARD' — it matches every SKU of the brand."""
    tokens = [_fold(t) for t in _tokenize_phrase(text)]
    if not tokens:
        return True
    brand_parts = _brand_token_set(brands)
    return all(token in brand_parts for token in tokens)


def _is_motor_spec_line(text: str) -> bool:
    folded = _fold(text)
    return folded.startswith("motor") or folded.startswith("motor:") or ": motor" in folded


def _line_qualifies_as_title(text: str, brands: list[str], *, from_hypothesis: bool) -> bool:
    if brands and _is_brand_only_line(text, brands):
        return False
    if from_hypothesis:
        return True
    brand_fold = _brand_token_set(brands)
    folded = _fold(text)
    if brand_fold and any(part in folded for part in brand_fold):
        return not _is_motor_spec_line(text)
    tokens = _tokenize_phrase(text)
    if text.isupper() and len(tokens) >= 2:
        return True
    has_model_ref = any(
        re.search(r"[a-z]\d{2}", _fold(tok), re.IGNORECASE) for tok in tokens
    )
    return bool(text.isupper() and len(tokens) >= 3 and has_model_ref)


def _title_like_lines(analysis: StoryVisualUnderstanding) -> list[str]:
    brands = [str(b) for b in (analysis.visible_brands or []) if b]
    lines: list[str] = []
    seen: set[str] = set()
    sources: tuple[tuple[list[str], bool], ...] = (
        (list(analysis.visible_references or []), True),
        (list(analysis.model_hypotheses or []), True),
        (list(analysis.collection_hypotheses or []), True),
        (list(analysis.visible_text or []), False),
    )
    for source, from_hypothesis in sources:
        for raw in source:
            text = str(raw or "").strip()
            if len(text) < 8 or text in seen:
                continue
            if not _line_qualifies_as_title(text, brands, from_hypothesis=from_hypothesis):
                continue
            seen.add(text)
            lines.append(text)
    return lines


def model_line_search_tokens(analysis: StoryVisualUnderstanding) -> list[str]:
    """Distinctive tokens from the model title block (C63, Sealander, Rocks)."""
    brands = [str(b) for b in (analysis.visible_brands or []) if b]
    brand_parts = _brand_token_set(brands)
    for line in _title_like_lines(analysis):
        if _is_motor_spec_line(line):
            continue
        if brands and _is_brand_only_line(line, brands):
            continue
        parts = [
            part.strip().strip(":.,;")
            for part in str(line).replace("/", " ").replace("-", " ").split()
        ]
        strong = [
            part
            for part in parts
            if _fold(part) not in brand_parts
            and len(part) >= 2
            and _fold(part) not in {"relogio", "relogios", "mm"}
            and _fold(part) not in _GENERIC_TITLE_SKIP
            and not _fold(part).endswith("mm")
            and not (part.isdigit() and len(part) <= 3)
        ]
        if len(strong) >= 2:
            weak, rest = [], []
            for part in strong:
                (weak if _fold(part) in _WEAK_TITLE_TOKENS else rest).append(part)
            return (rest + weak)[:4]
    return []


def evaluate_text_evidence(analysis: StoryVisualUnderstanding) -> StoryEvidenceProfile:
    """Path 1: everything legible on the Story image."""
    profile = StoryEvidenceProfile()
    profile.model_lines = _title_like_lines(analysis)
    for line in profile.model_lines:
        for token in _tokenize_phrase(line):
            key = _fold(token)
            if len(key) >= 2 and not key.isdigit():
                profile.positive_tokens.add(key)
            if token.isdigit() and len(token) == 2:
                profile.size_mm = token
    for raw in analysis.visible_text or []:
        m = _SIZE_RE.search(str(raw))
        if m:
            profile.size_mm = m.group(1)
    for sku in analysis.visible_skus or []:
        for token in _tokenize_phrase(sku):
            profile.positive_tokens.add(_fold(token))
    if analysis.readable_text_confidence:
        profile.text_path_score = min(1.0, float(analysis.readable_text_confidence))
    elif profile.model_lines:
        profile.text_path_score = 0.85
    return profile


def evaluate_structure_evidence(
    analysis: StoryVisualUnderstanding,
    profile: StoryEvidenceProfile,
) -> StoryEvidenceProfile:
    """Path 2: structured vision fields (colors, shape, mechanism)."""
    for raw in analysis.dial_colors or []:
        for syn in _COLOR_SYNONYMS.get(_fold(raw), (_fold(raw),)):
            profile.colors.add(syn)
    for region in analysis.product_regions or []:
        dial = _fold(getattr(region, "dial_color", None))
        if dial:
            profile.colors.update(_COLOR_SYNONYMS.get(dial, (dial,)))
    for shape in analysis.case_shapes or []:
        profile.positive_tokens.add(_fold(shape))
    for mech in analysis.mechanisms_suggested or []:
        key = _fold(mech)
        if key:
            profile.positive_tokens.add(key)
    parts = 0
    hits = 0
    if analysis.visible_brands or analysis.logo_hypotheses:
        parts += 1
        hits += 1
    if profile.colors:
        parts += 1
        hits += 1
    if analysis.watch_count == 1:
        parts += 1
        hits += 1
    profile.structure_path_score = hits / parts if parts else 0.0
    return profile


def build_evidence_profile(analysis: StoryVisualUnderstanding) -> StoryEvidenceProfile:
    profile = evaluate_text_evidence(analysis)
    return evaluate_structure_evidence(analysis, profile)


def _listing_blob(candidate: StoryProductCandidate) -> str:
    parts: list[str] = []
    for reason in candidate.match_reasons or []:
        if str(reason).startswith("listing:"):
            parts.append(str(reason)[8:])
    return _fold(" ".join(parts))


def score_catalog_overlap(
    candidate: StoryProductCandidate,
    profile: StoryEvidenceProfile,
) -> tuple[float, list[str], list[str]]:
    """Path 3: score one catalog row against merged evidence."""
    blob = _listing_blob(candidate)
    if not blob:
        return 0.0, [], []

    positives = [t for t in profile.positive_tokens if len(t) >= 3]
    hits = sum(1 for token in positives if token in blob)
    hit_ratio = hits / max(len(positives), 1)

    reasons: list[str] = []
    conflicts: list[str] = []
    score = hit_ratio * 0.55

    if profile.colors and any(color in blob for color in profile.colors):
        score += 0.15
        reasons.append("color_overlap")

    if profile.size_mm:
        if profile.size_mm in blob or f"{profile.size_mm} mm" in blob:
            score += 0.12
            reasons.append(f"size_{profile.size_mm}mm")
        elif "39" in blob and profile.size_mm == "36":
            score += 0.04

    brand_parts = {
        "ward",
        "christopher",
        "bulova",
        "citizen",
        "laco",
        "baltic",
        "mido",
        "seiko",
        "tissot",
    }
    family_tokens = [
        t
        for t in positives
        if re.match(r"c\d{2}", t)
        or (
            len(t) >= 4
            and t not in brand_parts
            and t not in {"automatic", "automatico", "mecanico", "rocks"}
        )
    ]
    if family_tokens and not any(t in blob for t in family_tokens):
        score -= 0.35
        conflicts.append("model_family_miss")

    for marker in _VARIANT_MARKERS:
        if marker in blob and marker not in profile.positive_tokens:
            if not any(marker in _fold(line) for line in profile.model_lines):
                score -= 0.32
                conflicts.append(f"unseen_variant:{marker}")

    # Distinctive model-line words (Rocks, Leipzig, Summer) missing from listing.
    distinctive = [
        t
        for t in positives
        if t not in {"automatic", "automatico", "mecanico", "quartz", "ward", "relogio"}
        and len(t) >= 4
    ]
    missing_distinctive = [t for t in distinctive if t not in blob]
    if distinctive and missing_distinctive:
        miss_ratio = len(missing_distinctive) / len(distinctive)
        score -= 0.08 * miss_ratio
        if miss_ratio >= 0.5:
            conflicts.append("model_line_partial")

    score = max(0.0, min(1.0, score))
    if hits >= 2:
        reasons.append(f"text_hits:{hits}")
    return score, reasons, conflicts


def rerank_candidates_with_consensus(
    candidates: list[StoryProductCandidate],
    analysis: StoryVisualUnderstanding,
) -> list[StoryProductCandidate]:
    """Reorder by consensus without mutating Tray confidence scores."""
    if not candidates:
        return candidates
    profile = build_evidence_profile(analysis)
    if not profile.positive_tokens and not profile.colors and not profile.model_lines:
        return candidates

    sort_rows: list[tuple[float, StoryProductCandidate]] = []
    for cand in candidates:
        catalog_score, reasons, conflicts = score_catalog_overlap(cand, profile)
        consensus = (
            profile.text_path_score * 0.4
            + profile.structure_path_score * 0.2
            + catalog_score * 0.4
        )
        nudge = consensus * 0.12 - 0.04 * len(conflicts)
        sort_rows.append((cand.score + nudge, cand))

    sort_rows.sort(key=lambda row: (-row[0], row[1].product_id))
    top = sort_rows[0][1] if sort_rows else None
    second = sort_rows[1][1] if len(sort_rows) > 1 else None
    log_event(
        "story_match_decider",
        {
            "candidate_count": len(candidates),
            "text_path": round(profile.text_path_score, 3),
            "structure_path": round(profile.structure_path_score, 3),
            "model_lines": profile.model_lines[:2],
            "top_product_id": top.product_id if top else None,
            "top_tray_score": round(top.score, 3) if top else None,
            "second_product_id": second.product_id if second else None,
            "second_tray_score": round(second.score, 3) if second else None,
            "size_mm": profile.size_mm,
        },
    )
    return [row[1] for row in sort_rows]


def try_resolve_tied_candidates(
    candidates: list[StoryProductCandidate],
    analysis: StoryVisualUnderstanding,
    *,
    margin: float = 0.12,
    min_consensus: float = 0.58,
) -> StoryProductCandidate | None:
    """When Tray scores tie, pick a winner if evidence consensus is clear."""
    if len(candidates) < 2:
        return None
    profile = build_evidence_profile(analysis)
    if profile.text_path_score < 0.75 and not profile.model_lines:
        return None

    scored: list[tuple[float, StoryProductCandidate, list[str]]] = []
    for cand in candidates:
        catalog_score, reasons, conflicts = score_catalog_overlap(cand, profile)
        consensus = (
            profile.text_path_score * 0.4
            + profile.structure_path_score * 0.2
            + catalog_score * 0.4
        )
        if conflicts:
            consensus -= 0.05 * len(conflicts)
        scored.append((consensus, cand, conflicts))

    scored.sort(key=lambda row: (-row[0], row[1].product_id))
    top_score, top, top_conflicts = scored[0]
    second_score, second, second_conflicts = scored[1] if len(scored) > 1 else (0.0, None, [])
    gap = top_score - second_score

    tray_tied = (
        len(candidates) > 1 and abs(candidates[0].score - candidates[1].score) < 0.02
    )
    if tray_tied and profile.text_path_score >= 0.85 and second is not None:
        top_has_variant = any(str(c).startswith("unseen_variant:") for c in top_conflicts)
        second_has_variant = any(str(c).startswith("unseen_variant:") for c in second_conflicts)
        if top_has_variant and not second_has_variant:
            return second
        if second_has_variant and not top_has_variant:
            return top

    if tray_tied and gap >= 0.03 and top_score >= min_consensus:
        return top
    if profile.text_path_score >= 0.85 and gap >= 0.03:
        return top
    if margin and gap >= margin * 0.45 and top_score >= min_consensus:
        return top
    return None
