"""Bind short qualification answers to the current search, never contact tastes."""
import re
from app.catalog.retrieval.text import _fold


def apply_qualification_answer(interpretation, text, slot=None):
    if interpretation.domain != "commerce":
        return
    prefs = interpretation.preferences
    folded = _fold(text)
    if slot in {"purchase_purpose", "gender", "style", "mechanism"} and re.fullmatch(
        r"(?:nao sei|sem preferencia|tanto faz|qualquer um)[.! ]*", folded
    ):
        if slot not in prefs.explicit_no_preferences:
            prefs.explicit_no_preferences.append(slot)
        if slot in {"style", "mechanism"}:
            setattr(prefs, slot, None)
        if slot == "gender":
            prefs.recipient = None
            prefs.attributes = [a for a in prefs.attributes if a not in {"masculino", "feminino", "unissex"}]
        return
    purpose = None
    if re.search(r"\b(?:para presente|pra presente|e presente|presentear)\b", folded) or (slot == "purchase_purpose" and folded == "presente"):
        purpose = "gift"
    elif re.search(r"\b(?:uso pessoal|para mim|pra mim)\b", folded):
        purpose = "self"
    if purpose:
        prefs.attributes = [a for a in prefs.attributes if not a.startswith("qual:purchase_purpose:")]
        prefs.attributes.append("qual:purchase_purpose:" + purpose)
    if slot == "style" and folded.strip(".! ") in {
        "aviador", "classico", "dress", "esportivo", "field", "mergulho", "militar", "piloto"
    }:
        prefs.style = folded.strip(".! ")
    if slot == "gender":
        from app.catalog.specs.preference_normalize import extract_stated_gender
        gender = extract_stated_gender(text)
        if gender:
            prefs.recipient = gender
            prefs.attributes = [a for a in prefs.attributes if a not in {"masculino", "feminino", "unissex"}]
            prefs.attributes.append(gender)
    if slot == "mechanism":
        movement = {"automatico": "automatic", "mecanico": "mechanical", "quartzo": "quartz", "quartz": "quartz"}.get(folded.strip(".! "))
        if movement:
            prefs.mechanism = movement


def qualification_known(interpretation):
    from app.catalog.specs.preference_normalize import preference_gender_label
    prefs = interpretation.preferences
    known = {k for k in ("style", "mechanism", "crystal") if getattr(prefs, k, None)}
    if preference_gender_label(interpretation):
        known.add("gender")
    if any(a.startswith("qual:purchase_purpose:") for a in prefs.attributes):
        known.add("purchase_purpose")
    return known
