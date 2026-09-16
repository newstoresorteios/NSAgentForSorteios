"""Editable policy repairs from the ninth real-model candidate."""
import json
from pathlib import Path
from build_regression_continuity_catalog import field

ROOT=Path(__file__).resolve().parents[1]

def build():
    catalog={f['key']:f for f in json.loads((ROOT/'sql/seeds/operator_catalog.json').read_text(encoding='utf-8'))}
    consent=dict(catalog['handoffConsentRules'])
    rules=json.loads(consent['default'])
    rules['decline']=r'(?:nao(?: obrigado| obrigada| precisa| quero)?|prefiro continuar por aqui|quero continuar por aqui)(?: (?:obrigado|obrigada|quero continuar por aqui|prefiro continuar por aqui|podemos continuar por aqui))?'
    rules['transferPromise']=r'(?<!nao )\b(?:vou|irei|estou|ja (?:te )?(?:encaminhei|transferi))\b.{0,22}\b(?:passar|encaminhar|transferir|transferindo|encaminhando)\b.{0,38}\b(?:equipe|atendente|humano|pessoa|joao)\b'
    consent['default']=json.dumps(rules,ensure_ascii=False)
    judge=dict(catalog['message.judge_commercial_policy'])
    addition=' O campo upon_request indica preço sob consulta. Mesmo com available_for_purchase=1 e estoque positivo, preço ausente ou zero não permite concluir a compra automática; não confunda essa limitação com ausência física de estoque. O texto availability pode ser citado como observação da ficha, sem garantir entrega ou possibilidade de compra. Uma menção atribuída à ficha, junto da limitação de preço, não confirma disponibilidade imediata. Uma resposta de link ou foto não precisa repetir todos os alertas de preço quando não faz uma nova promessa comercial.'
    if addition.strip() not in judge['default']:
        judge['default']+=addition
    followup=dict(catalog['conversationFollowupRules'])
    media_rules=json.loads(followup['default'])
    media_rules['resumeMediaReference']=r'\b(?:esse|essa|este|esta|desse|dessa)\b.{0,35}\b(?:sugeriu|mencionou|mostrou|indicou)\b'
    media_rules['purchaseCommit']=r'\b(?:comprar|compra|carrinho|pagar|pagamento|fechar|levar|pedido)\b'
    followup['default']=json.dumps(media_rules,ensure_ascii=False)
    return [consent,judge,followup,
        field('message.handoff_declined_continue','Continuidade após recusa de transferência',
            'Tudo bem, seguimos por aqui. Posso esclarecer as informações e políticas disponíveis. Solicitações que dependem da equipe, como avaliação, devolução ou reembolso, precisam de um atendente para execução; não consigo concluir essas ações pelo chat automático.',
            'Respeita a recusa, explica os limites e mantém o atendimento automático ativo.',target='message'),
        field('message.named_customer_known_request','Nome recebido com opções conhecidas',
            'Obrigado, {name}. Seguimos com seu pedido de {topic}.',
            'Reconhece o nome sem assumir intenção de compra nem escolher uma opção da lista.',target='message')]

if __name__=='__main__':
    fields=build()
    p=ROOT/'sql/seeds/operator_catalog.json'
    catalog={f['key']:f for f in json.loads(p.read_text(encoding='utf-8'))}
    catalog.update({f['key']:f for f in fields})
    p.write_text(json.dumps(list(catalog.values()),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (ROOT/'sql/seeds/regression_evidence_repairs.json').write_text(json.dumps(fields,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
