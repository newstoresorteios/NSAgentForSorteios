import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.configuration.runtime import current_bundle, bind_bundle, reset_bundle
from app.llm.role_policy import configured_call, active_policy, apply_chat_controls

@pytest.mark.parametrize('call_type,role', [('visual_fingerprint','vision'), ('story_visual_analysis','vision'), ('decision_fallback_chat','interpretation'), ('product_selection_shadow','interpretation'), ('checkout_repair','interpretation'), ('learning_reflect','review')])
def test_actual_call_types_keep_their_role(call_type, role):
    from app.llm.role_policy import role_for
    assert role_for(call_type) == role

@pytest.fixture
def configured():
    bundle=deepcopy(current_bundle())
    bundle['values']['modelRolePolicies']=json.dumps({'review':{'model':'configured-model','reasoning_effort':'low','max_output_tokens':600,'timeout_seconds':12}})
    bundle['values']['modelCapabilityRegistry']=json.dumps({'configured-model':{'responses':True,'structured_outputs':True,'reasoning_efforts':['low'],'chat_completions':True,'tools':True,'chat_tools':False}})
    token=bind_bundle(bundle,None)
    yield bundle
    reset_bundle(token)

def test_isolation_and_transport_controls(configured):
    with configured_call('judge','baseline',structured=True) as (model,policy):
        assert model=='configured-model'
        kwargs={}
        apply_chat_controls(kwargs,model)
        assert kwargs=={'reasoning_effort':'low','max_completion_tokens':600}
        with pytest.raises(ValueError,match='chat_transport'):
            apply_chat_controls({},model,tools=True)
    assert active_policy.get() is None
    with configured_call('decision','baseline') as (model,policy):
        assert model=='baseline' and policy is None

def test_invalid_effort_fails_before_transport(configured):
    configured['values']['modelRolePolicies']=json.dumps({'review':{'model':'configured-model','reasoning_effort':'none'}})
    with pytest.raises(ValueError,match='reasoning_effort'):
        with configured_call('judge','baseline'): pass

def test_primary_transport_rejected_before_provider(configured):
    caps=json.loads(configured['values']['modelCapabilityRegistry'])
    caps['configured-model']['responses']=False
    configured['values']['modelCapabilityRegistry']=json.dumps(caps)
    with pytest.raises(ValueError,match='primary_transport'):
        with configured_call('judge','baseline'): pass

def test_chat_role_without_specific_limit_keeps_global_bound(configured,monkeypatch):
    configured['values']['modelRolePolicies']=json.dumps({'review':{'model':'configured-model'}})
    monkeypatch.setattr('app.config.get_settings',lambda:SimpleNamespace(openai_max_output_tokens=700))
    with configured_call('judge','baseline'):
        kwargs={};apply_chat_controls(kwargs,'configured-model')
    assert kwargs['max_completion_tokens']==700

@pytest.mark.asyncio
async def test_gateway_uses_role_and_timeout(configured,monkeypatch):
    from app.llm.openai_gateway import generate_text_output
    method=AsyncMock(return_value=SimpleNamespace(text='ok'))
    monkeypatch.setattr('app.llm.openai_gateway.get_openai_gateway',lambda:SimpleNamespace(generate_text=method))
    await generate_text_output(model='baseline',call_type='judge',messages=[])
    assert method.call_args.kwargs['model']=='configured-model'
    assert method.call_args.kwargs['timeout_seconds']==12
    assert active_policy.get() is None
