"""Build operator-editable recovery/availability policies and their migration."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def field(key, label, default, description, target='policy'):
    return dict(key=key, target=target, type='textarea', label=label,
                group='Continuidade e disponibilidade', description=description,
                maxLength=12000, default=default)


def build():
    return [
        field('readyToShipCategoryIds', 'Categorias de pronta entrega', '403',
              'IDs das categorias oficiais que identificam pronta entrega, separados por vírgula.'),
        field('catalogAvailabilityRules', 'Exigência de pronta entrega', json.dumps({
            'require': r'\b(?:a pronta entrega|somente pronta entrega|so pronta entrega|preciso.{0,25}pronta entrega)\b',
            'release': r'\b(?:pode ser|aceito|pode vir).{0,25}(?:encomenda|sem pressa)\b',
            'historyTurns': 12,
            'question': r'\b(?:qual.{0,20}prazo|prazo de entrega|pronta entrega ou|sob encomenda|quanto tempo.{0,20}(?:chega|entrega))\b',
        }, ensure_ascii=False), 'JSON para preservar e retirar a exigência do cliente de pronta entrega.'),
        field('catalogHistoryRecoveryRules', 'Recuperar produto mencionado', json.dumps({
            'followup': r'\b(?:foto|fotos|imagem|imagens|link|esse que.{0,15}sugeriu|desse|dele|dessa|dela)\b',
            'reference': r'\b((?=[A-Z0-9.\-]*\d)[A-Z][A-Z0-9]+(?:[-.][A-Z0-9]+)*)\b',
            'historyTurns': 20,
        }), 'Recupera uma única referência no histórico e confirma no catálogo antes de responder. Não executa compra.'),
        field('message.catalog_ready_unconfirmed', 'Modelo sem pronta entrega confirmada',
              'Localizei {name}, mas não confirmei pronta entrega para esse modelo. A ficha informa: {availability}. Link oficial: {url}',
              'Explica quando o modelo existe mas não atende à pronta entrega solicitada.', target='message'),
        field('message.catalog_ready_unknown_note', 'Disponibilidade não informada',
              'disponibilidade não informada', 'Texto quando a ficha não informa disponibilidade.', target='message'),
        field('message.catalog_ready_no_match', 'Sem pronta entrega confirmada na busca',
              'Não confirmei uma opção com pronta entrega que atenda aos critérios na consulta atual. Posso verificar opções sob encomenda, se esse prazo servir para você.',
              'Usada após consulta sem resultado elegível; não afirma ausência definitiva no catálogo.', target='message'),
        field('message.catalog_availability_item', 'Prazo da ficha do produto',
              '{name}: {availability}. O prazo final para seu endereço deve ser confirmado no checkout. {url}',
              'Resume a informação da ficha sem confundir disponibilidade ou despacho com entrega garantida.', target='message'),
    ]


if __name__ == '__main__':
    fields = build()
    (ROOT/'sql/seeds/regression_availability_fixes.json').write_text(json.dumps(fields, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    path = ROOT/'sql/seeds/operator_catalog.json'
    catalog = {f['key']: f for f in json.loads(path.read_text(encoding='utf-8'))}
    catalog.update({f['key']: f for f in fields})
    path.write_text(json.dumps(list(catalog.values()), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
