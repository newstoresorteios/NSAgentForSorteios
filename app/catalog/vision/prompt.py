"""Vision schema and commercial identification prompt."""

from __future__ import annotations

import unicodedata

from pydantic import BaseModel, Field

from app.catalog.retrieval.aliases import vision_commercial_model_rules

IMAGE_IDENTIFY_INSTRUCTIONS = f"""\
Você identifica relógios em fotos enviadas por clientes da NewStore (loja de relógios).

Extraia o máximo de identidade comercial visível — NÃO fique só em marca + cor:

- marca (brand)
- modelo / linha / coleção (model): nome no mostrador ou linha comercial
  (ex.: Intra-Matic, Prospex Sea Samurai, Sealander, Khaki Field, Ecce Lys, C63)
- referência comercial se aparecer legível (ex.: H38446732, SRPL13K1, C63-36ADA4-S00P0-B0)
- cor do MOSTRADOR (dial) no campo color — só a cor do disco (branco, preto, rosa…)
- acabamento da CAIXA/pulseira no campo case_finish (aço/prata, preto ion, ouro, titânio…),
  separado do mostrador
- funções/atributos visíveis em features[]: chronograph/cronógrafo (submostradores +
  botões), diver/mergulho, GMT, automatic, quartz, etc.

Regras:
- is_watch=false se a imagem não for um relógio de pulso.
- Não invente referência. Se não ler a ref, deixe reference=null.
- reference só quando houver código comercial legível. Nunca coloque cor/descrição
  do mostrador em reference — use color.
- Em color: NÃO inclua pulseira, couro, caixa prata/aço.
  Ex.: mostrador preto + caixa aço → color="preto", case_finish="aço" (ou "prata").
- Em model: priorize linha/coleção COMERCIAL usada em e-commerce BR, não só o texto
  literal do mostrador.
  {vision_commercial_model_rules()}
  * Se vir só "AUTOMATIC" / "DIVER'S 200m" / "CHRONO" e NÃO souber a linha comercial,
    ainda assim inclua a função em features — não descarte.
- Se houver submostradores ou botões de cronógrafo, features DEVE incluir "cronógrafo".
- Se o mostrador tiver a palavra AUTOMATIC / AUTOMÁTICO, features DEVE incluir "automático"
  (não confunda com variantes manuais/mecânicas da mesma linha).
- Nunca retorne só brand+color quando a linha ou a função estiver legível na foto.
- confidence entre 0 e 1 conforme legibilidade.
- Preferir nomes comerciais usados em e-commerce BR.
- Se houver legenda do cliente, use-a só como dica complementar — a imagem manda.
"""


class ImageProductIdentification(BaseModel):
    is_watch: bool = True
    brand: str | None = None
    model: str | None = None
    reference: str | None = None
    color: str | None = None
    case_finish: str | None = None
    features: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str | None = None


def normalize_feature_label(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    folded = "".join(
        char
        for char in unicodedata.normalize("NFKD", text).lower()
        if not unicodedata.combining(char)
    )
    if "crono" in folded or "chrono" in folded:
        return "Cronógrafo"
    if "diver" in folded or "mergulho" in folded or "200m" in folded or "200 m" in folded:
        return "Mergulho"
    if "gmt" in folded:
        return "GMT"
    if "automatic" in folded or "automatico" in folded:
        return "Automático"
    if "quartz" in folded:
        return "Quartz"
    return text
