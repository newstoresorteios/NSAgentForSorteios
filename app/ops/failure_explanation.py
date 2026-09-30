"""Explain a known failure without claiming the agent cannot analyze images."""
from __future__ import annotations

from app.models import AgentResult


_FAILURES = {
    "image_identify_failed": "Não consegui concluir a análise dessa imagem agora.",
    "image_identify_low_confidence": "Não consegui confirmar com segurança o modelo dessa foto.",
    "image_catalog_ambiguous": "A foto pode corresponder a mais de um modelo; não consegui confirmar qual é o seu.",
    "image_catalog_unconfirmed": "Não consegui confirmar uma correspondência segura entre essa foto e o catálogo.",
    "image_catalog_search_incomplete": "Não consegui concluir a comparação dessa foto com o catálogo agora.",
    "story_route_error": "Não consegui concluir a análise desse Story agora.",
    "audio_transcription_failed": "Não consegui entender esse áudio com segurança.",
    "category_adapter_error": "Não consegui concluir a consulta ao catálogo agora.",
    "tray_authentication_failed": "Não consegui concluir a consulta à loja agora.",
    "tray_connection_failed": "Não consegui concluir a consulta à loja agora.",
}


def failure_explanation(result: AgentResult) -> str | None:
    reason = str(result.safety_reason or "").removeprefix("integration_failure:")
    return _FAILURES.get(reason)


def apply_failure_explanation(result: AgentResult) -> AgentResult:
    explanation = failure_explanation(result)
    if not explanation:
        return result
    reason = str(result.safety_reason or "").removeprefix("integration_failure:")
    help_text = (
        "Se tiver outra foto ou o link da publicação, pode enviar por aqui."
        if reason.startswith(("image_", "story_")) else
        "Você também pode enviar sua pergunta por escrito." if reason == "audio_transcription_failed" else ""
    )
    result.response_metadata["failure_explanation"] = explanation
    result.response_metadata["failure_next_step"] = help_text
    result.reply_text = " ".join(part for part in (explanation, help_text) if part)
    return result
