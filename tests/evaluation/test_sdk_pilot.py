import json
from copy import deepcopy
import pytest

agents = pytest.importorskip('agents', reason='SDK pilot has an isolated optional environment')
from agents import Model, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall
from app.configuration.runtime import current_bundle, bind_bundle, reset_bundle
from app.agents.sdk_pilot import run_readonly_pilot


class OfflinePolicyModel(Model):
    async def get_response(self, *args, **kwargs):
        return ModelResponse(output=[ResponseFunctionToolCall(
            id='offline-tool-1',call_id='offline-call-1',type='function_call',name='lookup_policy',
            arguments=json.dumps({'question':'Qual é a política de troca dos relógios?'}))],
            usage=Usage(),response_id='offline-response-1')

    async def stream_response(self, *args, **kwargs):
        raise AssertionError('No streaming transport in the offline pilot')
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_sdk_executes_readonly_tool_without_network_or_mutations():
    bundle=deepcopy(current_bundle());bundle['values']['agentsSdkPilotEnabled']=True
    token=bind_bundle(bundle,None)
    try:
        result=await run_readonly_pilot('Qual é a política de troca?',model=OfflinePolicyModel())
        docs=json.loads(result.final_output)['documents']
        assert any(d['slug']=='trocas-e-devolucoes' for d in docs)
        assert len(result.raw_responses)==1
        assert sum(r.usage.total_tokens for r in result.raw_responses)==0
    finally:
        reset_bundle(token)
