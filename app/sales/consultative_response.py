"""Turn-scoped sales guidance. Customer preferences never become product facts."""
from __future__ import annotations

from app.commerce.commerce_context import CommerceConversationState
from app.models import ProductPreferences, SalesInterpretation
from app.sales.preference_state import merge_preferences


SALES_CONVERSATION_POLICY = """<sales_conversation>
Conduza a conversa como um vendedor atento, seguindo a persona publicada.
SALES_CONVERSATION reúne o objetivo e as preferências do cliente neste atendimento;
é contexto, não evidência de características, preço, estoque ou entrega do produto.
- Responda ao pedido atual e use a ocasião, o prazo e as preferências já informados
  para explicar sua orientação. Reconheça o motivo de forma breve e natural quando
  isso ajudar; não recite todo o histórico nem repita a mesma abertura a cada turno.
- Se o cliente pediu opções ou catálogo e a consulta já trouxe resultados, mostre-os
  agora. Não condicione a apresentação a perguntas opcionais nem peça autorização
  para fazer uma busca que já foi feita. Explique por que uma opção merece atenção
  apenas com características confirmadas em FACTS; diferencie opinião de fato.
- Evite despejar fichas completas: destaque o que ajuda a decidir neste pedido,
  mantenha a ordem das opções e os links. Uma dúvida pontual merece resposta pontual.
- Não pergunte de novo os campos em do_not_ask_again, nem force uma escolha sobre
  algo a que o cliente já disse ser indiferente. Uma pergunta opcional só cabe após
  atender ao pedido, se ajudar a próxima decisão; pode ser melhor encerrar sem pergunta.
- Tamanho ou medida do pulso podem ajudar quando relevantes, mas não são obrigatórios
  para mostrar opções. Não reinicie a qualificação quando o cliente adiciona um detalhe.
- Em feedback de incompreensão ou repetição, reconheça brevemente e corrija a ação:
  retome o que já foi pedido. Não acrescente mais perguntas de perfil.
- Continue ajudando a escolher quando houver informação suficiente. Encaminhamento
  humano depende do pedido do cliente ou de uma necessidade real do fluxo, não é um
  encerramento automático de toda resposta com produtos.
- Prazo desejado é uma restrição do cliente. Anúncio na lista de pronta entrega não
  comprova estoque final nem chegada antes do evento. Não prometa reserva, desconto,
  pagamento ou entrega sem evidência e autorização do fluxo correspondente.
</sales_conversation>"""


def sales_conversation_brief(
    interpretation: SalesInterpretation | None,
    state: CommerceConversationState | None,
) -> dict:
    # A new objective must not acquire tastes or deadlines from the previous sale.
    continuing = interpretation is None or interpretation.references_previous_context
    prior = getattr(state, "active_preferences", {}) if continuing else {}
    current_subject = interpretation.subject.model_dump(exclude_none=True) if interpretation else {}
    subject = {key: current_subject.get(key)
               or (prior or {}).get('subject_' + key)
               for key in ('brand', 'model', 'reference', 'ean')}
    subject = {key: value for key, value in subject.items() if value}
    fields = set(ProductPreferences.model_fields)
    prior = {key: value for key, value in (prior or {}).items() if key in fields}
    current = interpretation.preferences.model_dump(exclude_none=True) if interpretation else {}
    merged = merge_preferences(prior, current)
    known = {key: value for key, value in merged.items()
             if key != "explicit_no_preferences" and value not in (None, "", [], {})}
    answered = set(merged.get("explicit_no_preferences") or [])
    for key in list(subject):
        if key in answered and (not current_subject.get(key) or key in current.get('explicit_no_preferences', [])):
            subject.pop(key)
    answered.update(subject)
    for key in known:
        if key != "attributes":
            answered.add("budget" if key in {"budget_min", "budget_max"} else key)
    for attribute in known.get("attributes", []):
        if str(attribute).startswith("qual:"):
            parts = str(attribute).split(":", 2)
            if len(parts) == 3:
                answered.add(parts[1])
        elif str(attribute).startswith('case_size:'):
            answered.add('case_size')
        elif str(attribute).startswith('required_strap_material:'):
            answered.add('strap')
    return {
        "goal": interpretation.goal if interpretation else None,
        "requested_information": list(interpretation.information_needed) if interpretation else [],
        "known_preferences": known,
        "known_subject": subject,
        "do_not_ask_again": sorted(answered),
        "customer_feedback": interpretation.conversation_feedback if interpretation else None,
        "answer_strategy": interpretation.resolved_answer_strategy() if interpretation else None,
    }


def needs_consultative_response(brief: dict, product_count: int) -> bool:
    return bool(
        product_count > 1
        or brief["known_preferences"]
        or set(brief["do_not_ask_again"]) - set(brief["known_subject"])
        or brief["customer_feedback"]
        or brief["goal"] in {"recommend", "compare", "discover"}
    )
