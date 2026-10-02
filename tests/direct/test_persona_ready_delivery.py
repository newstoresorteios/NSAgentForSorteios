import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from app.direct.persona import persona_context
from app.direct.tools import DirectTools
from app.models import IncomingMessage
from app.tray.tray_adapter_client import TrayAdapterClient
from app.tray.tray_circuit_breaker import reset_tray_circuit_breaker_for_tests


def test_persona_preserves_full_instructions_and_examples_without_runtime_secrets():
    instructions = 'Identidade e regras publicadas. ' * 1000
    persona = NS(active_persona=NS(instructions=instructions), persona_version_id=17,
                 chatbo_profile={'examples': ['Atenda com atenção'], 'recommendation_rules': ['Consulte pronta entrega'],
                                 'agent_configuration': {'api_key': 'SECRET'}})
    context = persona_context(persona)
    assert json.loads(context['content'])['instructions'] == instructions
    assert 'Atenda com atenção' in context['content']
    assert 'SECRET' not in context['content']
    persona.active_persona.instructions += ' Nova regra.'
    assert persona_context(persona)['sha256'] != context['sha256']


@pytest.mark.asyncio
async def test_ready_stock_cannot_use_wrong_catalog():
    adapter = NS(search_products=AsyncMock())
    executor = DirectTools(incoming=IncomingMessage(), history=[], documents=[], adapter=adapter)
    result = await executor.execute('search_products', '{"query":"relógio","ready_stock":true}')
    assert result['error'] == 'use_ready_delivery_source'
    adapter.search_products.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 401, 503, 'timeout'])
async def test_ready_delivery_contract_preserves_evidence_and_ignores_old_preferences(status):
    reset_tray_circuit_breaker_for_tests()
    seen = []
    payload = {'success': True, 'complete': True, 'source': 'https://www.newstorerj.com/pronta-entrega',
               'stockConfirmed': False, 'evidenceType': 'public_listing', 'checkedAt': '2026-10-02T00:00:00Z',
               'products': [{'name': 'Relógio', 'url': 'https://www.newstorerj.com/relogio', 'listedAvailable': True}]}
    def respond(request):
        seen.append(request)
        if status == 'timeout':
            raise httpx.ReadTimeout('private details', request=request)
        return httpx.Response(status, json=payload if status == 200 else {'error': 'private details'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        adapter = TrayAdapterClient('https://adapter.test', 'private-token', http)
        adapter.max_get_attempts = 1
        executor = DirectTools(incoming=IncomingMessage(text='Quais modelos têm pronta entrega?'),
            history=[{'role': 'user', 'content': 'Seiko até 2500'}], documents=[], adapter=adapter)
        result = await executor.execute('search_ready_delivery', '{"query":"pronta entrega"}')
    request = seen[0]
    assert request.method == 'GET' and request.url.path == '/internal/ready-delivery'
    assert dict(request.url.params) == {'query': 'pronta entrega'}
    assert request.headers['authorization'] == 'Bearer private-token'
    assert request.content == b'' and request.extensions['timeout']['read'] == 12
    if status == 200:
        assert result['data'] == payload
    else:
        assert result['error'] == 'commerce_unavailable'
        assert 'private details' not in json.dumps(result)
    reset_tray_circuit_breaker_for_tests()
