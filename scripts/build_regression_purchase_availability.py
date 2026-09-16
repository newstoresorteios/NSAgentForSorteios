"""Operator-managed recognition for availability questions before purchase."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

if __name__=='__main__':
    p=ROOT/'sql/seeds/operator_catalog.json'
    catalog={f['key']:f for f in json.loads(p.read_text(encoding='utf-8'))}
    field=dict(catalog['catalogAvailabilityRules'])
    rules=json.loads(field['default'])
    addition=r'\b(?:disponiv(?:el|eis)|disponibilidade|em estoque)\b'
    if addition not in rules['question']:
        rules['question']='(?:'+rules['question']+')|(?:'+addition+')'
    field['default']=json.dumps(rules,ensure_ascii=False)
    catalog[field['key']]=field
    p.write_text(json.dumps(list(catalog.values()),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (ROOT/'sql/seeds/regression_purchase_availability.json').write_text(json.dumps([field],ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
