"""Customer display-name heuristics — WhatsApp/Brevo nicks vs given names.

Kept out of greeting_policy so qualification, webhook parse, and addressivity
share one rule without importing sales greeting copy.
"""

from __future__ import annotations

import re
import unicodedata


def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(ch for ch in value if not unicodedata.combining(ch))


_NICK_COLOR_WORDS = frozenset({
    "blue", "red", "black", "white", "green", "pink", "gold", "silver",
    "dark", "light", "neo", "pro", "max", "mini", "grey", "gray",
    "orange", "yellow", "purple",
})
_NICK_DESCRIPTOR_WORDS = frozenset({
    "razor", "wolf", "dragon", "shadow", "killer", "master", "king", "queen",
    "devil", "ghost", "ninja", "storm", "fire", "ice", "star", "moon", "sun",
    "sky", "rock", "steel", "blade", "hunter", "player", "gamer", "bot",
})
_COMMON_FIRST_NAMES = frozenset({
    "joao", "maria", "jose", "ana", "pedro", "paulo", "carlos", "luis", "marcos",
    "fernando", "rafael", "gabriel", "bruno", "felipe", "rodrigo", "marcelo",
    "andre", "lucas", "matheus", "guilherme", "ricardo", "daniel", "eduardo",
    "fabio", "renato", "roberto", "sergio", "antonio", "francisco", "julia",
    "juliana", "camila", "patricia", "fernanda", "amanda", "beatriz", "carolina",
    "larissa", "mariana", "natalia", "renata", "silvia", "vanessa", "aline",
    "claudia", "helena", "isabela", "leticia", "priscila", "sandra", "tatiana",
    "viviane",
})


def looks_like_whatsapp_nick(name: str | None) -> bool:
    """True for Brevo/WhatsApp profile labels that are not real given names."""
    text = str(name or "").strip()
    if not text:
        return False
    if re.search(r"\d", text):
        return True
    if re.search(r"[^\w\s\-'.À-ÿ]", text):
        return True
    words = [_fold(part) for part in text.split() if part.strip()]
    if not words:
        return False
    if len(words) == 1:
        word = words[0]
        return len(word) <= 2 or (word not in _COMMON_FIRST_NAMES and len(word) > 12)
    if words[0] in _COMMON_FIRST_NAMES:
        return False
    nick_markers = _NICK_COLOR_WORDS | _NICK_DESCRIPTOR_WORDS
    if any(word in nick_markers for word in words):
        return True
    return False


def resolve_address_name(
    *,
    preferred_name: str | None = None,
    checkout_name: str | None = None,
    account_name: str | None = None,
    whatsapp_profile_name: str | None = None,
) -> str | None:
    """Prefer durable/legal identity over WhatsApp nick for addressivity."""
    for candidate in (
        preferred_name,
        checkout_name,
        account_name,
        whatsapp_profile_name,
    ):
        text = str(candidate or "").strip()
        if not text:
            continue
        if (
            candidate is whatsapp_profile_name
            and looks_like_whatsapp_nick(text)
        ):
            continue
        return text
    return None
