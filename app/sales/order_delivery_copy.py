"""Explain recorded order deadlines without turning an old estimate into a promise."""
from datetime import datetime
from zoneinfo import ZoneInfo
import re

from app.catalog.retrieval.text import fold_text


def complete_order_delivery_copy(result, text):
    facts = result.commercial_data or {}
    query = fold_text(text)
    if (not facts.get('order_id') or not facts.get('status')
            or not isinstance(facts.get('tracking'), dict)
            or not re.search(r'prazo|previsao|envio|entrega|ja passou|mais.*pedido', query)
            or re.search(r'pagar|pagamento|boleto|\bpix\b', query)):
        return result
    tracking = facts['tracking']
    raw_date = str(tracking.get('estimated_delivery_date') or '')
    deadline = None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y'):
        try:
            deadline = datetime.strptime(raw_date[:10], fmt).date()
            break
        except ValueError:
            continue
    reply = f"Seu pedido {facts['order_id']} está com status “{facts['status']}”."
    overdue = deadline and deadline < datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    if overdue:
        reply += (f" A previsão cadastrada, {deadline:%d/%m/%Y}, já passou. "
                  'Ela não serve como nova previsão de chegada. Não há uma data atualizada confirmada nesta consulta. '
                  'Quer que a equipe confira o atraso e a nova previsão?')
    elif deadline:
        reply += f' A previsão estimada de entrega cadastrada é {deadline:%d/%m/%Y}; não é a data de postagem.'
    else:
        reply += ' A consulta não trouxe uma previsão de entrega confirmada.'
    if tracking.get('tracking_url'):
        reply += '\nRastreio: ' + str(tracking['tracking_url'])
    updated = result.model_copy(deep=True)
    updated.reply_text = reply
    updated.response_metadata.update(goal='after_sales', response_source='order_delivery_status',
                                     factual_fallback_text=reply)
    if overdue:
        updated.response_metadata['handoff'] = {'offer': True, 'required': False}
    return updated
