"""Commercial catalog aliases and token tables (dial color, Prospex, Sky Pilot).

``COMMERCIAL_LINES`` is the single table for Vision prompt bullets and
compiler/search aliases. Do not duplicate SKU heuristics in ``vision/prompt.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

_MODEL_STOPWORDS = frozenset(
    {
        "relogio",
        "watch",
        "automatico",
        "automatic",
        "quartz",
        "cronografo",
        "chronograph",
        "mm",
        "com",
        "para",
        "the",
        "and",
    }
)
# Color/material adjectives are useful but must not block identity matches.
_OPTIONAL_MODEL_TOKENS = frozenset(
    {
        "branco",
        "preto",
        "rosa",
        "azul",
        "verde",
        "dourado",
        "prata",
        "cinza",
        "vermelho",
        "amarelo",
        "laranja",
        "bege",
        "claro",
        "escuro",
        "titanio",
        "aco",
        "ouro",
        "couro",
        "borracha",
        "carbon",
        "carbono",
        "ceramica",
        "nylon",
        "pulseira",
        "mostrador",
        "dial",
        "bezel",
    }
)
# Soft descriptors that must never block a match even for single-token models.
_DESCRIPTOR_MODEL_TOKENS = frozenset(
    {
        "claro",
        "escuro",
        "mostrador",
        "dial",
        "bezel",
        "face",
        "caixa",
        # Gender never participates in model identity / exact probes.
        "feminino",
        "feminina",
        "masculino",
        "masculina",
        "unissex",
        "unisex",
        "lady",
        "ladies",
        "dama",
        "damas",
        "women",
        "woman",
        "men",
        "man",
    }
)


@dataclass(frozen=True)
class CommercialLine:
    """Brand + dial cues → catalog ``model`` used in Tray titles."""

    catalog_model: str
    brand: str
    aliases: tuple[str, ...]
    vision_rule: str
    mislabels: frozenset[str] = frozenset()
    soft_identity: frozenset[str] = frozenset()


COMMERCIAL_LINES: tuple[CommercialLine, ...] = (
    CommercialLine(
        catalog_model="Prospex Sea Samurai",
        brand="Seiko",
        aliases=(
            "Sea Samurai",
            "Prospex Sea Samurai",
            "King Turtle",
            "Prospex King Turtle",
        ),
        soft_identity=frozenset({"diver", "divers", "mergulho", "200m", "200"}),
        vision_rule=(
            '* Seiko com logo Prospex (X) + AUTOMATIC + DIVER\'S 200m (sem "Save the Ocean" / '
            'Monster / GMT no mostrador): use model="Prospex Sea Samurai" — esse é o nome '
            "de catálogo (ex.: SRPL13K1). Coloque Mergulho/Automático em features.\n"
            '  * Se "King Turtle" / "Turtle" / "Samurai" estiver escrito/legível, use esse nome.'
        ),
    ),
    CommercialLine(
        catalog_model="Promaster Sky Pilot",
        brand="Citizen",
        aliases=(
            "Sky Pilot",
            "Promaster Sky Pilot",
            "Promaster Sky Pilot Eco Drive",
            "Citizen Promaster Sky Pilot",
            "JV2000-51L",
            "JV2000",
        ),
        mislabels=frozenset(
            {
                "navihawk",
                "navi hawk",
                "blueangels",
                "blue angels",
                "skyhawk",
                "sky hawk",
            }
        ),
        vision_rule=(
            "* Citizen Promaster com Eco-Drive + mostrador ana-digi (janela digital / "
            'CALENDAR) + luneta com régua de cálculo (slide rule): use '
            'model="Promaster Sky Pilot" (catálogo JV2000-51L etc.). NÃO use Navihawk '
            'salvo se "NAVIHAWK" estiver escrito de forma legível no mostrador.'
        ),
    ),
)


def commercial_line_by_model(catalog_model: str) -> CommercialLine | None:
    wanted = catalog_model.casefold()
    for line in COMMERCIAL_LINES:
        if line.catalog_model.casefold() == wanted:
            return line
    return None


def vision_commercial_model_rules() -> str:
    """Prompt bullets generated from ``COMMERCIAL_LINES`` — do not hardcode SKUs elsewhere."""
    return "\n  ".join(line.vision_rule for line in COMMERCIAL_LINES if line.vision_rule)


_PROSPEX_LINE = commercial_line_by_model("Prospex Sea Samurai")
_SKY_PILOT_LINE = commercial_line_by_model("Promaster Sky Pilot")
# Dial boilerplate Tray omits for Seiko Prospex divers (Sea Samurai / King Turtle).
_PROSPEX_DIVER_SOFT_IDENTITY = (
    _PROSPEX_LINE.soft_identity if _PROSPEX_LINE is not None else frozenset()
)
_PROSPEX_DIVER_COMMERCIAL_ALIASES: tuple[str, ...] = (
    _PROSPEX_LINE.aliases if _PROSPEX_LINE is not None else ()
)
_PROMASTER_SKY_PILOT_ALIASES: tuple[str, ...] = (
    _SKY_PILOT_LINE.aliases if _SKY_PILOT_LINE is not None else ()
)
_PROMASTER_SKY_MISLABELS = (
    _SKY_PILOT_LINE.mislabels if _SKY_PILOT_LINE is not None else frozenset()
)
# Strap/case materials from Vision — useful for ranking, never AND-required.
_ACCESSORY_COLOR_TOKENS = frozenset(
    {
        "pulseira",
        "strap",
        "bracelet",
        "bege",
        "cream",
        "creme",
        "prata",
        "silver",
        "aco",
        "titanio",
        "couro",
        "leather",
        "borracha",
        "nylon",
        "ouro",
        "gold",
        "caixa",
        "case",
        "carcasa",
    }
)
_DIAL_COLOR_TOKENS = frozenset(
    {
        "branco",
        "preto",
        "rosa",
        "azul",
        "verde",
        "dourado",
        "cinza",
        "vermelho",
        "amarelo",
        "laranja",
        "pink",
        "blue",
        "black",
        "white",
        "green",
        "navy",
        "marinho",
        "red",
        "gold",
        "silver",
        "gray",
        "grey",
        "yellow",
        "orange",
    }
)
# PT ↔ EN (and close variants) so "azul" matches catalog "blue".
_COLOR_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"azul", "blue", "navy", "marinho"}),
    frozenset({"preto", "black"}),
    frozenset({"branco", "white"}),
    frozenset({"verde", "green"}),
    frozenset({"vermelho", "red", "vermelha", "amora"}),
    frozenset({"rosa", "pink", "rose"}),
    frozenset({"amarelo", "yellow"}),
    frozenset({"laranja", "orange"}),
    frozenset({"cinza", "gray", "grey"}),
    frozenset({"dourado", "gold", "golden"}),
    frozenset({"prata", "silver"}),
)
_DIAL_COLOR_RIVAL_TOKENS: frozenset[str] = frozenset(
    token for group in _COLOR_ALIAS_GROUPS for token in group
)
_ACCESSORY_NAME_TOKENS = frozenset(
    {
        "strap",
        "pulseira",
        "caixa",
        "box",
        "kit",
        "tool",
        "capa",
        "case",
        "fone",
        "cabo",
        "adapter",
        "adaptador",
    }
)
_FEATURE_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "cronografo": ("cronografo", "chronograph", "chrono", "cronograph"),
    # NewStore titles Prospex divers as Sea Samurai / King Turtle, not "Diver's 200m".
    "mergulho": (
        "mergulho",
        "diver",
        "divers",
        "dive",
        "200m",
        "samurai",
        "turtle",
        "sea samurai",
        "king turtle",
    ),
    "gmt": ("gmt",),
    "pulseira_integrada": ("prx", "integrad"),
    "acabamento_escovado": ("escovad", "rajad", "brushed", "prata"),
}

__all__ = [
    "COMMERCIAL_LINES",
    "CommercialLine",
    "commercial_line_by_model",
    "vision_commercial_model_rules",
    "_ACCESSORY_COLOR_TOKENS",
    "_ACCESSORY_NAME_TOKENS",
    "_COLOR_ALIAS_GROUPS",
    "_DESCRIPTOR_MODEL_TOKENS",
    "_DIAL_COLOR_RIVAL_TOKENS",
    "_DIAL_COLOR_TOKENS",
    "_FEATURE_SEARCH_ALIASES",
    "_MODEL_STOPWORDS",
    "_OPTIONAL_MODEL_TOKENS",
    "_PROMASTER_SKY_MISLABELS",
    "_PROMASTER_SKY_PILOT_ALIASES",
    "_PROSPEX_DIVER_COMMERCIAL_ALIASES",
    "_PROSPEX_DIVER_SOFT_IDENTITY",
]
