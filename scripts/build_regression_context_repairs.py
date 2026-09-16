"""Operator-editable corrections discovered by candidate eight replays."""
import json
from pathlib import Path
from build_regression_continuity_catalog import field

ROOT=Path(__file__).resolve().parents[1]


def build():
    catalog={f['key']:f for f in json.loads((ROOT/'sql/seeds/operator_catalog.json').read_text(encoding='utf-8'))}
    recovery=dict(catalog['conversationRepairPhrases'])
    recovery['default']='\n'.join(dict.fromkeys([*recovery['default'].splitlines(),
        'não entendeu nada','nao entendeu nada','oque isso tem haver','o que isso tem a ver','o que isso tem haver']))
    followup=dict(catalog['conversationFollowupRules'])
    rules=json.loads(followup['default'])
    rules['interestOnly']=r'(?:me interessei|tenho interesse|gostei)(?:\s+(?:nesse|neste|desse|deste|dele|dela|dessa|nessa|nele|nela)(?:\s+(?:modelo|relogio|produto))?)?[.!?]*'
    rules['alternativeSearch']=r'\b(?:outro|outra|outros|outras|alternativas?)\b'
    followup['default']=json.dumps(rules,ensure_ascii=False)
    preflight=dict(catalog['conversationPreflightRules'])
    routing=json.loads(preflight['default'])
    additions=[
        {'pattern':r'\b(?:quero|gostaria de|pretendo|preciso) vender (?:meu|minha|um|uma)\b','action':'handoff','message':'handoff_offer'},
        {'pattern':r'\bconcorrente\b.{0,45}\b(?:golpista|fraude|difam)', 'action':'reply','message':'competitor_comparison_policy'},
    ]
    routing.extend(x for x in additions if x not in routing)
    preflight['default']=json.dumps(routing,ensure_ascii=False)
    critique=dict(catalog['message.critique_availability_evidence'])
    critique['default']=critique['default'].replace('upon_request verdadeiro confirma disponibilidade sob consulta;', 'upon_request verdadeiro confirma preço sob consulta e impede concluir uma compra automática, mas não comprova ausência de estoque;')
    return [recovery,followup,preflight,critique,
        field('message.catalog_unavailable_on_request','Produto com preço sob consulta',
              'Encontrei esse modelo, mas o preço está sob consulta e a compra ainda não está liberada. Não consigo confirmar o valor nem a entrega para uma compra agora. A observação cadastrada na ficha é: “{availability}”.',
              'Distingue ausência de preço válido de ausência de estoque. O prazo textual da ficha não libera a compra.',target='message'),
        field('catalogModelFamilyRules','Famílias de modelos reconhecidas no contexto',json.dumps([
            {'pattern':r'\bmk\s*0?2\b','model':'Aquascaphe mk2','brand':'Baltic','brandPattern':r'\bbaltic\b'},
            {'pattern':r'\bmr\s*0?1\b','model':'mr01','brand':'Baltic','brandPattern':r'\bbaltic\b'},
            {'pattern':r'\baquascaphe\b','model':'aquascaphe','brand':'Baltic','brandPattern':r'\bbaltic\b'},
            {'pattern':r'\bspeedtimer\b','model':'speedtimer'},
            {'pattern':r'\bking\s+turtle\b','model':'kingturtle'},
            {'pattern':r'\bsamurai\b','model':'samurai'},
            {'pattern':r'\bopen\s*heart\b','model':'Open Heart'},
            {'pattern':r'\bheritage\b','model':'Heritage'},
            {'pattern':r'\bhydroconquest\b','model':'Hydroconquest'},
        ],ensure_ascii=False),'JSON de padrões, família canônica e marca opcional. Ajuda a preservar a linha nas mudanças de orçamento e respostas curtas.'),
        field('message.conversation_repair_shipping_city','Retomar seleção após pergunta de entrega',
              'Você tem razão: cidade e estado ajudam a calcular o frete, mas não mudam as características do relógio. Vou retomar os critérios que você informou.',
              'Reconhece o desvio de assunto antes de responder à consulta conhecida.',target='message'),
        field('message.competitor_comparison_policy','Comparação respeitosa com outras lojas',
              'Não posso fazer acusações sem evidência sobre outra loja. Sobre a New Store: somos uma importadora independente, com preço final incluindo os impostos e nota fiscal brasileira. Posso comparar características e condições confirmadas para ajudar na sua escolha.',
              'Posicionamento comercial editável; manter de acordo com a persona e políticas publicadas.',target='message'),
        field('message.named_customer_pending_request', 'Nome recebido com pedido conhecido',
              'Obrigado, {name}. Vou manter seu pedido de {topic}. Qual faixa de investimento você considera?',
              'Usada quando o cliente informa o nome, o assunto do relógio já é conhecido e ainda falta orçamento.',target='message'),
        field('message.catalog_requirements_price_on_request', 'Características confirmadas com preço sob consulta',
              'Encontrei o {product}, que confirma as características técnicas pedidas, mas está com preço sob consulta, sem valor válido na ficha. Assim, ainda não consigo confirmar uma opção para {criteria}.',
              'Preço sob consulta não comprova orçamento nem significa ausência de estoque. Não promete compra ou prazo.',target='message'),
    ]


if __name__=='__main__':
    fields=build()
    p=ROOT/'sql/seeds/operator_catalog.json'
    catalog={f['key']:f for f in json.loads(p.read_text(encoding='utf-8'))}
    catalog.update({f['key']:f for f in fields})
    p.write_text(json.dumps(list(catalog.values()),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (ROOT/'sql/seeds/regression_context_repairs.json').write_text(json.dumps(fields,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
