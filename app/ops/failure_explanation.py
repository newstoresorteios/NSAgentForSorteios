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
    "instagram_media_unviewable": "Não consegui acessar a mídia dessa publicação agora.",
    "audio_transcription_failed": "Não consegui entender esse áudio com segurança.",
    "category_adapter_error": "Não consegui concluir a consulta ao catálogo agora.",
    "tray_authentication_failed": "Não consegui concluir a consulta à loja agora.",
    "tray_connection_failed": "Não consegui concluir a consulta à loja agora.",
}

_STORY_FAILURES = {
    "ambiguous": "Não consegui confirmar com segurança as referências exatas nesse Story.",
    "not_found": "Não consegui confirmar uma correspondência segura entre esse Story e o catálogo.",
    "expired": "Não consegui acessar a mídia desse Story agora.",
    "failed": "Não consegui concluir a análise desse Story agora.",
}


def failure_explanation(result: AgentResult) -> str | None:
    reason = str(result.safety_reason or "").removeprefix("integration_failure:")
    # Status names are shared by other flows. Only a server-tagged Story may
    # use these explanations; an ambiguous order/product search is not a Story.
    if result.response_metadata.get("instagram_story") is True:
        status = result.response_metadata.get("story_match_status")
        if status in _STORY_FAILURES:
            return _STORY_FAILURES[status]
    return _FAILURES.get(reason)


def apply_failure_explanation(result: AgentResult) -> AgentResult:
    explanation = failure_explanation(result)
    if not explanation:
        return result
    reason = str(result.safety_reason or "").removeprefix("integration_failure:")
    story_status = (
        result.response_metadata.get("story_match_status")
        if result.response_metadata.get("instagram_story") is True else None
    )
    help_text = (
        "Você pode indicar um relógio pela cor ou posição, ou enviar um print mais nítido."
        if story_status == "ambiguous" else
        "Se tiver outra foto ou o link da publicação, pode enviar por aqui."
        if reason.startswith(("image_", "story_")) or reason == "instagram_media_unviewable" or story_status in _STORY_FAILURES else
        "Você também pode enviar sua pergunta por escrito." if reason == "audio_transcription_failed" else ""
    )
    result.response_metadata["failure_explanation"] = explanation
    result.response_metadata["failure_next_step"] = help_text
    result.reply_text = " ".join(part for part in (explanation, help_text) if part)
    return result
