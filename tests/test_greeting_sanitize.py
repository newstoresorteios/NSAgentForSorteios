from app.greeting_policy import sanitize_greeting_reply


def test_sanitize_greeting_strips_instruction_label():
    raw = (
        "Saudação padrão (adapte ao contexto): Olá! Eu sou o Crono, "
        "assistente virtual da New Store Relógios. Como posso te ajudar hoje?"
    )
    cleaned = sanitize_greeting_reply(raw)
    assert cleaned.startswith("Olá! Eu sou o Crono")
    assert "adapte ao contexto" not in cleaned.casefold()
    assert "saudação padrão" not in cleaned.casefold()


def test_sanitize_greeting_keeps_clean_text():
    text = "Olá! Eu sou o Crono, assistente virtual da New Store Relógios. Como posso te ajudar hoje?"
    assert sanitize_greeting_reply(text) == text
