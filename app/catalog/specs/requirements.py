"""Technical requirements with operator-managed vocabulary and three-state evidence."""
from __future__ import annotations

import html
import json
import re
from typing import Any

from app.catalog.retrieval.text import fold_text
from app.configuration.runtime import policy, message
from app.models import SalesInterpretation


def feature_rules() -> list[dict]:
    return json.loads(policy("catalogTechnicalFeatures"))


def _contains(text: str, term: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(fold_text(term)) + r"(?!\w)", fold_text(text)))


def _denied(text: str, rule: dict, policy_name: str) -> bool:
    return any(_contains(text, phrase.format(feature=alias))
               for phrase in json.loads(policy(policy_name)) for alias in rule["aliases"])


def normalize_requirements(interpretation: SalesInterpretation, message_text: str | None = None) -> dict[str, str]:
    prefs = interpretation.preferences
    rules = feature_rules()
    required: dict[str, str] = {}
    # Persisted public preference fields survive history/database round trips.
    for field in {r["field"] for r in rules}:
        raw = getattr(prefs, field, None)
        source = str(raw) if raw else " ".join(prefs.attributes or [])
        choices = [r for r in rules if r["field"] == field and
                   (raw == r["value"] or any(_contains(source, a) for a in r["aliases"]))]
        if len(choices) == 1:
            required[field] = choices[0]["value"]
        elif raw:
            required[field] = str(raw)
    text = str(message_text or "")
    for field in {rule["field"] for rule in rules}:
        choices = [rule for rule in rules if rule["field"] == field]
        relaxed = [r for r in choices if _denied(text, r, "catalogFeatureRelaxationPhrases")]
        if relaxed:
            required.pop(field, None)
            setattr(prefs, field, None)
            prefs.attributes = [a for a in prefs.attributes if not any(_contains(a, alias) for r in relaxed for alias in r["aliases"])]
        explicit = [r for r in choices if any(_contains(text, a) for a in r["aliases"])
                    and not _denied(text, r, "catalogFeatureNegationPhrases") and r not in relaxed]
        if len(explicit) == 1:
            required[field] = explicit[0]["value"]
        elif len(explicit) > 1:
            # Different values of one property are ambiguous, never silently last-wins.
            required[field] = "|".join(r["value"] for r in explicit)
        if field in required:
            setattr(prefs, field, required[field])
    interpretation._technical_requirements = required
    return required


def technical_requirements(interpretation: SalesInterpretation) -> dict[str, str]:
    return normalize_requirements(interpretation)


def feature_evidence(product: dict[str, Any], requirements: dict[str, str]) -> dict:
    rules = feature_rules()
    facts = []
    for field, wanted in requirements.items():
        choices = [r for r in rules if r["field"] == field]
        keys = list(dict.fromkeys(k for r in choices for k in r["evidenceFields"]))
        structured = " ".join(str(product.get(k) or "") for k in keys).strip()
        source = structured or " ".join(str(product.get(k) or "") for k in ("name", "description", "properties", "attributes"))
        source = html.unescape(re.sub(r"<[^>]+>", " ", source))
        observed = {r["value"] for r in choices
                    if (structured.strip() == r["value"] or any(_contains(source, a) for a in r["aliases"]))
                    and not _denied(source, r, "catalogFeatureNegationPhrases")}
        wanted_rule = next((r for r in choices if r["value"] == wanted), None)
        denied = bool(wanted_rule and _denied(source, wanted_rule, "catalogFeatureNegationPhrases"))
        compatible = set((wanted_rule or {}).get('compatibleValues') or [])
        supported = wanted in observed and observed <= ({wanted} | compatible)
        status = "matched" if supported and not denied else "mismatch" if observed or denied else "unknown"
        facts.append({"field":field, "required":wanted, "observed":sorted(observed), "status":status,
                      "source":"structured" if structured else "product_description", "product_id":str(product.get("id") or "")})
    status = "mismatch" if any(f["status"] == "mismatch" for f in facts) else "unknown" if any(f["status"] == "unknown" for f in facts) else "matched"
    return {"status":status, "facts":facts}


def criteria_label(interpretation: SalesInterpretation) -> str:
    required = technical_requirements(interpretation)
    labels = [message("catalog_requirement_line", label=r["label"]) for r in feature_rules() if required.get(r["field"]) == r["value"]]
    if interpretation.preferences.budget_max is not None:
        amount = f"{interpretation.preferences.budget_max:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        labels.append(message("catalog_required_budget", amount=amount))
    return ", ".join(labels)
