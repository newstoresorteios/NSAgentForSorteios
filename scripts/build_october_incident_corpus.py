"""Convert the audited snapshot to a sanitized, conversation-grouped evidence corpus.

This prepares test inputs. It never calls a model or asserts that a case passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def sanitize(text):
    value = str(text or '')
    # Preserve the identifier class with a synthetic valid CPF, never a customer document.
    value = re.sub(r'(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)', '52998224725', value)
    value = re.sub(r'(?<!\d)(?:\+?55\s?)?\(?\d{2}\)?[ -]?\d{4,5}[ -]\d{4}(?!\d)', '[phone]', value)
    value = re.sub(r'(?<!\d)55\d{10,11}(?!\d)', '[phone]', value)
    value = re.sub(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', '[email]', value)
    value = re.sub(r'https?://\S+', '[source_url]', value)
    return value


def build(snapshot):
    review = json.loads((snapshot / 'today_review.json').read_text(encoding='utf-8'))
    threads = json.loads((snapshot / 'today_threads.json').read_text(encoding='utf-8'))
    inbound = json.loads((snapshot / 'inbound.json').read_text(encoding='utf-8'))
    responses = json.loads((snapshot / 'responses.json').read_text(encoding='utf-8'))
    response_by_inbound = {}
    for response in responses:
        response_by_inbound.setdefault(response['inbound_id'], []).append(response)
    conversations = []
    for thread in threads:
        conversation = thread['conversation_id']
        identity = digest([thread['workspace_id'], thread['channel'], conversation])
        turns = sorted(thread['turns'], key=lambda t: (t['created_at'], t['id']))
        history = []
        for prior in sorted(inbound, key=lambda t: (t['created_at'], t['id'])):
            if (prior['conversation_id'] != conversation or prior.get('workspace_id') != thread['workspace_id']
                    or prior['created_at'] >= turns[0]['created_at']):
                continue
            history.append({'role': 'user', 'content': sanitize(prior['text'])})
            for answer in response_by_inbound.get(prior['id'], []):
                if answer.get('provider_send_ok'):
                    history.append({'role': 'assistant', 'content': sanitize(answer['reply_text']),
                                    'metadata': {'safety_reason': answer.get('safety_reason')}})
        steps = []
        for turn in turns:
            expected = review['turns'][str(turn['id'])]
            steps.append({'case_id': str(turn['id']), 'recorded_at': turn['created_at'],
                          'input': sanitize(turn['text']),
                          'requirements': [sanitize(expected['expected'])],
                          'historical_status': expected['status'],
                          'historical_observation': sanitize(expected['observed']),
                          'historical_responses': [{'response_id': r['id'],
                              'reply': sanitize(r['reply_text']), 'sent': r['provider_send_ok'],
                              'safety_reason': r.get('safety_reason')} for r in turn['responses']],
                          'requires_media_annotation': '[Imagem' in turn['text'] or
                              any((r.get('metadata') or {}).get('response_source') == 'instagram_story' for r in turn['responses']),
                          'candidate_result': None})
        conversations.append({'key': identity[:16], 'split': 'validation' if int(identity[:8], 16) % 4 == 0 else 'development',
                              'channel': thread['channel'], 'history': history[-24:], 'steps': steps})
    return {'schema_version': 1, 'source_date': '2026-10-01',
            'source_hashes': {name: hashlib.sha256((snapshot / name).read_bytes()).hexdigest()
                              for name in ('today_review.json', 'today_threads.json', 'inbound.json', 'responses.json')},
            'method': 'Audited historical inputs; grouped split; identifiers replaced; no candidate execution implied.',
            'review_provenance': 'Codex session semantic audit; not an independent human review',
            'turn_count': sum(len(c['steps']) for c in conversations), 'conversations': conversations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    corpus = build(args.snapshot)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'conversations': len(corpus['conversations']), 'turns': corpus['turn_count']}))


if __name__ == '__main__':
    main()
