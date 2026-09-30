from unittest.mock import AsyncMock
import pytest
import httpx
from app.models import IncomingMessage
from app.sales.ready_delivery import try_ready_delivery
from app.tray.tray_adapter_client import TrayAdapterClient, TrayAdapterError


@pytest.mark.asyncio
async def test_lookup_and_offer(monkeypatch):
    monkeypatch.setattr('app.persona.site_knowledge.STORE_PRONTA_ENTREGA_URL', lambda: 'https://www.newstorerj.com/pronta-entrega')
    lookup = AsyncMock(return_value={'success': True, 'complete': True, 'products': []})
    monkeypatch.setattr(TrayAdapterClient, 'search_ready_delivery', lookup)
    result = await try_ready_delivery(IncomingMessage(text='Tissot salmão a pronta entrega?', channel='instagram'))
    assert 'Não encontrei' in result.reply_text
    assert result.response_metadata['handoff']['offer'] is True
    lookup.side_effect = TrayAdapterError('unavailable')
    result = await try_ready_delivery(IncomingMessage(text='Tissot pronta entrega?', channel='instagram'))
    assert 'Não consegui consultar' in result.reply_text
    assert await try_ready_delivery(IncomingMessage(text='meu pedido de pronta entrega', channel='instagram')) is None


@pytest.mark.asyncio
async def test_internal_contract():
    def handle(req):
        assert req.method == 'GET' and req.url.path == '/internal/ready-delivery'
        assert req.url.params['query'] == 'Tissot pronta entrega'
        assert req.headers['Authorization'] == 'Bearer test-token'
        return httpx.Response(200, json={'success': True, 'products': [], 'complete': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await TrayAdapterClient(base_url='https://adapter.test', token='test-token', http_client=http).search_ready_delivery('Tissot pronta entrega')
    assert result['success']
