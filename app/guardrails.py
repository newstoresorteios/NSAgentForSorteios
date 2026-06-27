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
)

CURRENT_RAFFLE_KEYWORDS = (
    "sorteio atual",
    "rodada atual",
    "sorteio aberto",
    "premio atual",
    "prêmio atual",
)

RAFFLE_HISTORY_KEYWORDS = (
    "sorteios passados",
    "sorteio passado",
    "vencedor",
    "numero sorteado",
    "número sorteado",
    "meus numeros",
    "meus números",
    "participei",
    "participação",
    "participacao",
    "resultado do sorteio",
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


def detect_balance_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in BALANCE_KEYWORDS)


def detect_coupon_code_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in COUPON_CODE_KEYWORDS)


def detect_simulation_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in SIMULATION_KEYWORDS)


def detect_current_raffle_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in CURRENT_RAFFLE_KEYWORDS)


def detect_raffle_history_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in RAFFLE_HISTORY_KEYWORDS)


def detect_rules_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in RULES_KEYWORDS)


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
    return (
        "Para sua segurança, vou encaminhar esse atendimento para a equipe da New Store. "
        "Você também pode acessar sua conta pelo site oficial para consultar informações disponíveis."
    )
