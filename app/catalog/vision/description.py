"""A useful visual description is not evidence of an exact catalog reference."""
from app.configuration.runtime import message


_COLORS = {
    "ivory": "marfim", "cream": "creme", "white": "branco",
    "black": "preto", "blue": "azul", "green": "verde",
    "brown": "marrom", "red": "vermelho", "pink": "rosa",
    "gold": "dourado", "silver": "prateado", "grey": "cinza",
    "gray": "cinza", "beige": "bege",
}
_COLORS.update({value: value for value in tuple(_COLORS.values())})


def unresolved_photo_reply(reason: str, identified: dict | None) -> str:
    """Expose only bounded visual observations, never a model/SKU hypothesis."""
    fallback = message(reason)
    if not isinstance(identified, dict) or not identified.get("is_watch"):
        return fallback
    try:
        if float(identified.get("confidence") or 0) < .55:
            return fallback
    except (TypeError, ValueError):
        return fallback
    details = []
    for field, label in (("dial_color", "mostrador"), ("strap_color", "pulseira")):
        raw = identified.get(field) or (identified.get("color") if field == "dial_color" else None)
        color = _COLORS.get(str(raw or "").strip().casefold())
        if color:
            details.append(f"{label} em tom {color}")
    if not details:
        return fallback
    return "Pela foto, o relógio parece ter " + " e ".join(details) + ". " + fallback
