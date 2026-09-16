"""Operator-editable consent recognition and the handoff offer."""
import json
from pathlib import Path
from build_regression_continuity_catalog import field

human = r'(?:atendente|atendimento humano|humano|ser humano|pessoa|alguem|equipe|vendedor|vendas)'
rules = {
    'negative': r'\b(?:(?:nao|nem) (?:quero|precis\w*|gostaria|desejo|(?:me )?(?:transf\w*|encaminh\w*|cham\w*))|sem (?:um |uma )?(?:atendente|humano)|dispenso|depois|mais tarde)\b',
    'directFull': [rf'(?:um |uma )?{human}[.!?]*', rf'(?:por favor )?falar com (?:um |uma |o |a )?{human}(?: por favor)?[.!?]*'],
    'directSearch': [
        rf'\b(?:quero|preciso|gostaria|prefiro|posso|poderia|pode|tem como|desejo)\b.{{0,45}}\b(?:falar|conversar|atendimento|chamar|chame|chama|transferir|transfira|encaminhar|encaminhe)\b.{{0,30}}\b{human}\b',
        rf'\b(?:quero|preciso de|chame|chama) (?:um |uma |o |a )?{human}\b',
        rf'\b(?:me passa|me passe|me transfere|me transfira|me encaminha|me encaminhe).{{0,25}}\b{human}\b',
    ],
    'acceptance': r'(?:(?:sim|si|yes|ok|okay|certo|beleza|blz|pode|pode ser|isso|uhum|uhu|quero|quero sim|por favor|pf|pfv|faz favor|manda|pode mandar|pode encaminhar|pode transferir|pode chamar|encaminha|encaminhe)(?: (?:por favor|pf|pfv|pode encaminhar|pode transferir|pode chamar|pode mandar|pode ser))?)',
    'offerAll': [r'\b(?:posso|quer|gostaria|se quiser)\b', r'\b(?:encaminh\w*|transfer\w*|passar|coloco|falar|chamar)\b', r'\b(?:atendente|equipe|humano|vendedor|vendas|joao)\b'],
}
fields = [
    field('message.conversation_identity_contract', 'Identidade e continuidade da resposta',
          'Use o nome do cliente apenas quando confirmado por ele no histórico ou em STATE_FACTS.active_preferences.qualification_slots.customer_name. Nunca use nomes de atendentes, exemplos ou da persona como nome do cliente. Se o nome não estiver confirmado, omita o vocativo. Não peça novamente um nome já conhecido. Uma consulta de link, foto ou característica de um produto identificado deve manter esse produto; uma correção na redação não justifica buscar modelos diferentes. Diferencie cores de mostrador, luneta e pulseira: não infira que todas são iguais. Preserve os critérios explícitos do cliente, inclusive orçamento, ao retomar a conversa.',
          'Instrução de redação aplicada junto da persona, sem substituir fatos do catálogo.', target='message'),
    field('handoffConsentRules', 'Pedido e aceite de atendimento humano', json.dumps(rules, ensure_ascii=False),
          'Expressões sobre texto sem acentos. Recusas bloqueiam transferência; aceite exige oferta imediatamente anterior.'),
    field('message.handoff_offer', 'Oferta de transferência para atendente',
          'Para continuar com esse assunto, preciso da ajuda de um atendente. Quer que eu encaminhe seu atendimento para a equipe?',
          'Oferece atendimento humano sem afirmar transferência já realizada. A fila só é acionada após pedido ou aceite explícito.', target='message'),
]
if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    (root/'sql/seeds/handoff_consent_catalog.json').write_text(json.dumps(fields,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    p = root/'sql/seeds/operator_catalog.json'
    catalog = {f['key']:f for f in json.loads(p.read_text(encoding='utf-8'))}
    catalog.update({f['key']:f for f in fields})
    p.write_text(json.dumps(list(catalog.values()),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
