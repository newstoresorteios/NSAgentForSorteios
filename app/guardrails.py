from __future__ import annotations

BALANCE_KEYWORDS = (
    "saldo",
    "meu saldo",
    "consultar saldo",
    "ver saldo",
    "quanto tenho",
    "valor do cupom",
)

COUPON_CODE_KEYWORDS = (
    "codigo do cupom",
    "código do cupom",
    "numero do cupom",
    "número do cupom",
    "meu cupom",
    "codigo do cartao",
    "código do cartão",
    "cartao presente",
    "cartão presente",
)

SIMULATION_KEYWORDS = (
    "simular",
    "simulação",
    "simulacao",
    "quanto posso usar",
    "tabela de uso",
    "simulador",
)

AVAILABLE_NUMBERS_KEYWORDS = (
    "numeros disponiveis",
    "números disponíveis",
    "numeros disponiveis no sorteio",
    "números disponíveis no sorteio",
    "quais numeros estao disponiveis",
    "quais números estão disponíveis",
    "quais numeros disponiveis",
    "quais números disponíveis",
    "numeros livres",
    "números livres",
    "numeros abertos",
    "números abertos",
    "ver numeros do sorteio",
    "ver números do sorteio",
    "numeros do sorteio atual",
    "números do sorteio atual",
)

CURRENT_RAFFLE_KEYWORDS = (
    "sorteio atual",
    "rodada atual",
    "sorteio aberto",
    "qual sorteio esta aberto",
    "qual sorteio está aberto",
    "que sorteio esta aberto",
    "que sorteio está aberto",
    "sorteio esta aberto",
    "sorteio está aberto",
    "premio atual",
    "prêmio atual",
)

RAFFLE_HISTORY_KEYWORDS = (
    "sorteios passados",
    "sorteio passado",
    "ultimo sorteio",
    "último sorteio",
    "ultima participacao",
    "última participação",
    "vencedor",
    "numero sorteado",
    "número sorteado",
    "meus numeros",
    "meus números",
    "participei",
    "participação",
    "participacao",
    "resultado do sorteio",
    "sorteios que participei",
    "sorteio que participei",
)

RULES_KEYWORDS = (
    "como funciona",
    "regras",
    "faq",
    "duvida",
    "dúvida",
    "cartao presente",
    "cartão presente digital",
    "lotomania",
)

HUMAN_SUPPORT_KEYWORDS = (
    "falar com atendente",
    "falar com um atendente",
    "atendente humano",
    "atendimento humano",
    "falar com alguem",
    "falar com alguém",
    "falar com a equipe",
    "falar com vocês",
    "falar com voces",
    "quero um humano",
    "quero atendente",
    "preciso de ajuda",
    "contato de vendas",
    "falar com vendas",
    "equipe de vendas",
    "whatsapp da loja",
    "numero da loja",
    "número da loja",
    "telefone da loja",
    "contato da new store",
    "contato new store",
)


def detect_balance_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in BALANCE_KEYWORDS)


def detect_coupon_code_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in COUPON_CODE_KEYWORDS)


def detect_simulation_inquiry(text: str) -> bool:
    from .simulation import detect_purchase_simulation_inquiry

    normalized = (text or "").lower()
    if any(keyword in normalized for keyword in SIMULATION_KEYWORDS):
        return True
    return detect_purchase_simulation_inquiry(text)


def detect_current_raffle_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in CURRENT_RAFFLE_KEYWORDS)


def detect_available_numbers_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in AVAILABLE_NUMBERS_KEYWORDS)


def detect_raffle_history_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in RAFFLE_HISTORY_KEYWORDS)


def detect_last_participation_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    phrases = (
        "ultimo sorteio",
        "último sorteio",
        "ultima participacao",
        "última participação",
        "ultimo que participei",
        "último que participei",
    )
    return any(phrase in normalized for phrase in phrases)


def detect_rules_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in RULES_KEYWORDS)


def detect_human_support_request(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in HUMAN_SUPPORT_KEYWORDS)


BLOCKED_TOPICS = (
    "comprar número",
    "comprar numeros",
    "apostar",
    "aposta",
    "bet",
    "jogar dinheiro",
    "ganhar prêmio",
    "garantir prêmio",
)


def detect_blocked_request(text: str) -> str | None:
    normalized = (text or "").lower()
    for topic in BLOCKED_TOPICS:
        if topic in normalized:
            return f"blocked_topic:{topic}"
    return None


def default_safe_handoff() -> str:
    from .site_knowledge import HUMAN_SUPPORT_MESSAGE, SITE_URL

    return (
        "Para sua segurança, vou encaminhar esse atendimento para a equipe da New Store. "
        f"{HUMAN_SUPPORT_MESSAGE} "
        f"Você também pode acessar sua conta em {SITE_URL}."
    )
