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


def default_safe_handoff() -> str:
    return (
        "Para sua segurança, vou encaminhar esse atendimento para a equipe da New Store. "
        "Você também pode acessar sua conta pelo site oficial para consultar informações disponíveis."
    )
