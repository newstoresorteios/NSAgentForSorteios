"""Live multi-turn evaluation against the isolated, authenticated preview endpoint.

Never writes inbox/outbox or sends messages to customers. Credentials and signed
sessions stay local and are never included in the printed evidence.
"""
import argparse
import base64
import hashlib
import hmac
import json
import re
import subprocess
import tempfile
from pathlib import Path

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--vercel-cli', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--knowledge-evidence-file', help='Trusted published policy excerpt for grounding checks when already in persona')
    args = parser.parse_args()
    if not args.url.startswith('https://'):
        parser.error('HTTPS required')
    token = dotenv_values(args.env_file).get('ADMIN_API_TOKEN')
    if not token or token == '[SENSITIVE]':
        parser.error('Usable ADMIN_API_TOKEN required in ignored env file')
    session = None
    evidence = []

    def turn(text):
        nonlocal session
        body = {'workspace_id': args.workspace, 'text': text, 'session': session}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'request.json'
            path.write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
            result = subprocess.run(['node', args.vercel_cli, 'curl', '/api/test/direct',
                '--deployment', args.url, '--', '-sS', '--max-time', '100', '-X', 'POST',
                '-H', 'Content-Type: application/json', '-H', 'Authorization: Bearer '+token,
                '--data-binary', '@'+str(path)], capture_output=True, text=True, encoding='utf-8', timeout=120)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError('preview_invalid_response') from None
        if not data.get('ok'):
            raise RuntimeError('preview_failed: '+str(data.get('detail', 'unknown')))
        assert data['sent_to_customer'] is False
        session = data['session']
        evidence.append({'input': text, 'reply': data['reply_text'], 'image_url': data.get('image_url'),
                         'metrics': data['metrics']})
        Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'turn':len(evidence),'reply':data['reply_text'],'tools':data['metrics']['tools'],
                          'searches':data['metrics'].get('catalog_searches'), 'image':bool(data.get('image_url'))},ensure_ascii=False),flush=True)
        return data

    first = turn('Liste os primeiros 10 modelos à pronta entrega e informe quantos há no total. Quero percorrer a lista por páginas.')
    searches = first['metrics']['catalog_searches']
    assert searches and searches[-1]['total'] > searches[-1]['returned'], 'Expected paginated catalog'
    second = turn('Tem mais modelos? Me mostra outras opções.')
    assert any(s['offset'] > 0 for s in second['metrics']['catalog_searches']), 'Continuation did not advance'
    assert second['metrics']['catalog_searches'][-1]['snapshot_id'] == searches[-1]['snapshot_id'], 'Continuation changed snapshot'
    assert second['metrics']['catalog_searches'][-1]['total'] == searches[-1]['total'], 'Continuation changed total'
    turn('Tenho um casamento no próximo final de semana. Qual combina?')
    photo = turn('Me manda a foto do Longines Spirit azul que você mostrou antes.')
    assert photo.get('image_url'), 'Verified photo was not attached'
    assert 'prepare_product_image' in photo['metrics']['tools']

    # Simulate a remote-session rebuild while retaining the signed delivered test
    # history, as happens when Brevo rolls the thread ID. No production data changes.
    state = json.loads(base64.urlsafe_b64decode(session.rsplit('.', 1)[0]))
    state['previous']['direct_agent']['scope'] = 'simulate-provider-thread-rollover'
    raw = base64.urlsafe_b64encode(json.dumps(state,ensure_ascii=False).encode()).decode()
    session = raw+'.'+hmac.new(token.encode(),raw.encode(),hashlib.sha256).hexdigest()
    continued = turn('O Longines que você falou antes: pode mandar a foto e o link de novo?')
    assert continued.get('image_url') == photo['image_url'], 'Rollover lost product identity'
    assert 'sou o crono' not in continued['reply_text'].lower(), 'Repeated introduction after rollover'
    knowledge = turn('Consulte a base de conhecimento publicada e me diga como funciona a garantia da loja.')
    if 'search_knowledge' not in knowledge['metrics']['tools'] and knowledge['metrics']['knowledge_mode']!='file_search':
        assert args.knowledge_evidence_file, 'Provide published evidence to verify policy already in persona'
        expected=json.loads(Path(args.knowledge_evidence_file).read_text(encoding='utf-8'))
        reply=knowledge['reply_text'].casefold()
        source=expected['text'].casefold()
        assert all(term.casefold() in reply and term.casefold() in source for term in expected['required_phrases'])
        assert set(re.findall(r'\b\d+\b',reply)) <= set(re.findall(r'\b\d+\b',source)), 'Unpublished policy numbers'
        assert knowledge['metrics'].get('persona_sha256'), 'Missing published persona evidence'
    declined = turn('Não quero comprar nem falar com atendente agora, obrigado.')
    assert 'request_human' not in declined['metrics']['tools'], 'Refusal triggered handoff'
    print('PASS: pagination, continuation, occasion, photo, rollover, knowledge and refusal; no customer sends.',flush=True)


if __name__ == '__main__':
    main()
