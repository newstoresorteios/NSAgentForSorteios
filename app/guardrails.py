from __future__ import annotations

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


BALANCE_KEYWORDS = (
    "saldo",
    "meu saldo",
    "consultar saldo",
    "ver saldo",
    "quanto tenho",
    "valor do cupom",
    "cupom",
)


def detect_balance_inquiry(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in BALANCE_KEYWORDS)


def default_safe_handoff() -> str:
    return (
        "Para sua segurança, vou encaminhar esse atendimento para a equipe da New Store. "
        "Você também pode acessar sua conta pelo site oficial para consultar informações disponíveis."
    )
