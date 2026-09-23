"""SDK adapter that reserves the same durable campaign budget before transport."""
import asyncio
import inspect


def budgeted_model(delegate, *, model_name, output_limit, timeout_seconds, close=None):
    from agents import Model
    from app.evaluation.campaign_budget import reserve_evaluation_call

    class BudgetedModel(Model):
        async def aclose(self):
            if close is not None:
                await close()

        async def get_response(self, *args, **kwargs):
            bound = inspect.signature(Model.get_response).bind(self, *args, **kwargs)
            values = bound.arguments
            settings = values['model_settings']
            # The reserved output bound must be the bound actually sent.
            settings.max_tokens = output_limit
            payload = {
                'instructions': values.get('system_instructions'),
                'input': values.get('input'),
                'tools': [{'name': t.name, 'description': t.description,
                           'parameters': t.params_json_schema} for t in values.get('tools', [])],
            }
            if values.get('handoffs') or values.get('previous_response_id') or values.get('conversation_id') or values.get('prompt'):
                raise ValueError('sdk_pilot_unbounded_context_unsupported')
            if values.get('output_schema'):
                payload['schema'] = values['output_schema'].json_schema()
            await asyncio.to_thread(reserve_evaluation_call, model=model_name,
                                    messages=payload, output_limit=output_limit)
            return await asyncio.wait_for(delegate.get_response(*args, **kwargs), timeout=timeout_seconds)

        async def stream_response(self, *args, **kwargs):
            raise ValueError('sdk_pilot_streaming_disabled')
            yield  # pragma: no cover

    return BudgetedModel()


def configured_provider():
    """Only production factory: explicit role, no SDK implicit model or retries."""
    from agents import OpenAIResponsesModel
    from openai import AsyncOpenAI
    from app.config import get_settings
    from app.llm.role_policy import configured_call
    with configured_call('sdk_evaluation', get_settings().openai_model, tools=True) as (name, role):
        if not role or not role.max_output_tokens or not role.timeout_seconds:
            raise ValueError('sdk_pilot_explicit_role_budget_required')
        client = AsyncOpenAI(api_key=get_settings().openai_api_key, max_retries=0,
                             timeout=role.timeout_seconds)
        return budgeted_model(OpenAIResponsesModel(model=name, openai_client=client),
                              model_name=name, output_limit=role.max_output_tokens,
                              timeout_seconds=role.timeout_seconds, close=client.close), role
