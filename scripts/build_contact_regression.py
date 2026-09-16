"""Build private contact replay and multi-turn cases from a canonical export.

The source contains personal data. Neither source nor generated suite belongs in Git.
Expected behavior is reviewed independently of the historical assistant answers.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evaluation.regression_models import RegressionSuite
from app.evaluation.repository import cases_from_rows

ORACLES = {
    787: 'Consulta automático com safira até R$ 2.500. Distingue opção confirmada de preço sob consulta; não inventa preço nem transfere genericamente quando há evidência útil.',
    789: 'Consulta automático com safira até R$ 2.500. Se o preço está ausente, não afirma que cabe no orçamento ou está disponível para compra.',
    790: 'Fornece o link oficial do produto que acabou de mencionar, consultando o produto se necessário; não repete apenas a resposta da busca.',
    791: 'Fornece a foto oficial do produto mencionado ou uma limitação concreta com seu link oficial. Não pede ao cliente uma foto para identificar o produto já conhecido.',
    792: 'Resolve a referência ao produto sugerido e informa seus dados ou link sem reiniciar a identificação ou afirmar compra confirmada.',
    793: 'Entrega o link oficial do relógio mencionado para visualização, sem iniciar carrinho nem perguntar novamente qual produto já identificado.',
    794: 'Entrega o endereço oficial da loja publicado na persona/configuração; não repete a pergunta loja ou relógio, já respondida pelo cliente.',
    795: 'Mantém a referência ao produto sugerido, explica a situação atual se necessário e avança com informação concreta; interesse não confirma pedido ou pagamento.',
    796: 'Mantém o produto com as características técnicas solicitadas; não trata preço sob consulta como orçamento confirmado.',
    797: 'Reconhece o desencontro e retoma o pedido conhecido com informação concreta, sem pedir todos os critérios novamente.',
    798: 'Reconhece a reclamação de indisponibilidade e consulta alternativas ou explica a limitação real; não pede cidade/estado para corrigir a seleção de relógio.',
    799: 'Resolve relógio como o produto já mencionado e fornece o link oficial ou informação concreta da ficha, sem reiniciar a seleção.',
    800: 'Reconhece que cidade/estado não resolve a seleção do relógio e retoma os critérios conhecidos sem nova saudação ou pergunta genérica.',
}
MUTATIONS = ['create_cart', 'set_cart_item_quantity', 'create_order', 'cancel_order']
SEQUENCE_RELEVANCE_ORACLE = ('Responde à objeção sobre a relevância da resposta anterior gerada nesta execução '
    'e retoma os critérios conhecidos, sem nova saudação ou pergunta genérica. '
    'Se tiver pedido cidade/estado, reconhece que isso não resolve a seleção do relógio.')


def build(rows, workspace, baseline):
    rows = [r for r in rows if r.get('response_id') and r.get('reply_text')]
    cases = {c['source_response_id']: c for c in cases_from_rows(
        rows, workspace, limit=2000, history_turns=20)}
    scenarios = []

    def step(text, expected):
        return {'input': text, 'expected': {'requirements': [expected],
            'forbidden_tools': MUTATIONS,
            'forbidden_claims': ['Pagamento, pedido, reserva ou disponibilidade confirmada sem evidência.']}}

    for response_id, oracle in ORACLES.items():
        c = cases[response_id]
        scenarios.append({'key': f'contact_replay_{response_id}', 'category': 'contato_replay',
            'split': 'development', 'source_response_ids': [response_id],
            'channel': c['channel'], 'history': c['history'], 'initial_state': c['initial_state'],
            'recorded_at': c['recorded_at'], 'environment': 'live_readonly',
            'steps': [step(c['input'], oracle)]})

    # Preserve actual input order while subsequent history comes from this run.
    sequence = [789, 790, 791, 792, 793, 794, 795, 796, 797, 798, 799, 800]
    first = cases[sequence[0]]
    scenarios.append({'key': 'contact_sequence_latest', 'category': 'contato_sequencia',
        'split': 'development', 'source_response_ids': sequence, 'history': first['history'],
        'initial_state': first['initial_state'], 'recorded_at': first['recorded_at'],
        'environment': 'live_readonly', 'steps': [step(cases[i]['input'],
            SEQUENCE_RELEVANCE_ORACLE if i == 800 else ORACLES[i]) for i in sequence]})

    fixture = deepcopy(baseline['scenarios'][0]['simulation'])
    variations = [
        ('consulta_link_foto', [
            ('Até 2.500 reais, quais são automáticos e têm vidro de safira?', 'Apresenta o Orient Kamasu RA-AA0001B confirmado dentro do teto.'),
            ('Me passa o link desse Kamasu', 'Fornece o link oficial do Kamasu 91001 sem iniciar compra.'),
            ('E uma foto dele?', 'Fornece a imagem oficial do mesmo Kamasu; não pede imagem ao cliente.')]),
        ('sob_consulta_link', [
            ('Quero o Orient Mako RA-AA0002L, quanto custa?', 'Explica que o preço está sob consulta e não vende pelo valor zero.'),
            ('Mesmo assim quero ver o link', 'Fornece o link oficial do Mako 91008; preço ausente não impede consultar a página.'),
            ('Tem foto?', 'Fornece foto oficial do Mako 91008 ou seu link com uma limitação concreta de mídia.')]),
        ('indisponivel_alternativas', [
            ('O Orient Open Heart RA-AG0029N10B está disponível?', 'Explica que está indisponível para compra, apesar do estoque numérico.'),
            ('Então me mostra outro automático com safira até 2500', 'Busca outra opção e apresenta Kamasu 91001; não mantém Open Heart como exigência.')]),
        ('corrigir_link_loja', [
            ('Quero um automático com safira até 2500', 'Apresenta Kamasu 91001 dentro do teto.'),
            ('Me manda o link da loja', 'Entrega endereço oficial da loja, sem perguntar se é o link da loja ou do relógio.')]),
    ]
    for key, turns in variations:
        scenarios.append({'key': f'contact_{key}', 'category': 'contato_variacoes',
            'split': 'development', 'environment': 'simulated_commerce', 'simulation': deepcopy(fixture),
            'steps': [step(text, oracle) for text, oracle in turns]})
    return RegressionSuite(name='contato-8149-regressao', version=1,
        categories=['contato_replay', 'contato_sequencia', 'contato_variacoes'],
        configuration_overrides=baseline['configuration_overrides'], scenarios=scenarios)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'baseline', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--workspace', required=True)
    args = parser.parse_args()
    suite = build(json.loads(args.source.read_text(encoding='utf-8')), args.workspace,
                  json.loads(args.baseline.read_text(encoding='utf-8')))
    args.output.write_text(suite.model_dump_json(indent=2), encoding='utf-8')
    print(json.dumps({'scenarios': len(suite.scenarios), 'turns': sum(len(s.steps) for s in suite.scenarios),
                      'image_attachment_processing': 'not_exercised_by_text_replay'}))
