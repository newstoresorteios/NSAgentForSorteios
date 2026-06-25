from __future__ import annotations
from openai import OpenAI
from .config import get_settings
from .models import IncomingMessage, AgentResult
from .guardrails import detect_blocked_request, default_safe_handoff


SYSTEM_INSTRUCTIONS = """
Você é o NewStoreAgent, um atendente virtual cordial, objetivo e seguro.

Regras obrigatórias:
- Responda em português do Brasil.
- Seja curto, claro e prestativo.
- Não solicite dados sensíveis por WhatsApp.
- Não altere dados do cliente.
- Não prometa resultado, vantagem, ganho, prêmio ou benefício financeiro.
- Não incentive compras, apostas, jogos de azar ou participação em atividades restritas.
- Se o pedido exigir verificação de identidade, dados financeiros, saldo detalhado ou ação sensível, encaminhe para atendimento humano ou oriente o cliente a acessar a área logada do site.
- Quando não tiver certeza, diga que vai encaminhar para a equipe.

Você pode ajudar com:
- orientar o cliente a acessar a área logada;
- explicar passos gerais de suporte;
- registrar intenção de atendimento;
- responder dúvidas institucionais simples.
""".strip()


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def build_agent_input(message: IncomingMessage, customer_context: dict) -> str:
    return f"""
Mensagem recebida via WhatsApp:
- Nome informado: {message.sender_name or 'não informado'}
- Telefone presente: {'sim' if message.sender_phone else 'não'}
- Texto: {message.text}

Contexto mínimo do cadastro:
{customer_context}

Responda com uma mensagem segura para WhatsApp.
""".strip()


def generate_agent_reply(message: IncomingMessage, customer_context: dict) -> AgentResult:
    settings = get_settings()

    blocked_reason = detect_blocked_request(message.text)
    if blocked_reason:
        return AgentResult(
            reply_text=default_safe_handoff(),
            intent="handoff",
            handoff_required=True,
            safety_reason=blocked_reason,
        )

    if not settings.openai_api_key:
        return AgentResult(
            reply_text="Recebemos sua mensagem. A equipe da New Store vai dar continuidade ao atendimento.",
            intent="fallback_no_openai_key",
            handoff_required=True,
            safety_reason="openai_api_key_missing",
        )

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.create(
        model=settings.openai_model,
        instructions=SYSTEM_INSTRUCTIONS,
        input=build_agent_input(message, customer_context),
    )

    reply = _truncate(response.output_text or default_safe_handoff(), settings.max_reply_chars)
    return AgentResult(
        reply_text=reply,
        intent="general_support",
        handoff_required=False,
    )
