"""Catalog-search readiness: a subject is a specific product, not a browse query."""

from __future__ import annotations

import unicodedata

from app.models import SalesInterpretation


def mentioned_watch_brands(text: str | None) -> list[str]:
    folded = unicodedata.normalize("NFKD", (text or "").casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    known = (
        "hamilton",
        "baltic",
        "tissot",
        "citizen",
        "seiko",
        "bulova",
        "orient",
        "casio",
        "mido",
        "omega",
        "longines",
        "oris",
        "certina",
        "tudor",
        "zenith",
        "breitling",
        "panerai",
        "iwc",
        "rolex",
        "tag heuer",
        "christopher ward",
    )
    found: list[str] = []
    display = {
        "tag heuer": "TAG Heuer",
        "christopher ward": "Christopher Ward",
        "bulova": "Bulova",
        "orient": "Orient",
        "casio": "Casio",
        "mido": "Mido",
    }
    for brand in known:
        if brand in folded and brand not in found:
            found.append(display.get(brand, brand.title()))
    return found


def specific_product_lock(interpretation: SalesInterpretation) -> bool:
    subject = interpretation.subject
    if subject.reference or subject.ean:
        return True
    model = str(subject.model or "").strip()
    if not model:
        return False
    model_fold = model.casefold()
    brand_fold = str(subject.brand or "").casefold()
    if brand_fold and model_fold == brand_fold:
        return False
    leftover = model_fold
    for hit in mentioned_watch_brands(model):
        leftover = leftover.replace(hit.casefold(), " ")
    for token in ("relógio", "relogio", "watch"):
        leftover = leftover.replace(token, " ")
    leftover = " ".join(leftover.split())
    if not leftover:
        return False
    from app.memory.context_resume import is_non_model_query

    if is_non_model_query(leftover):
        return False
    return True
