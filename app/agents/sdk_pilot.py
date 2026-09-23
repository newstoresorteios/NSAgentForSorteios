"""Isolated SDK pilot: read-only policy retrieval, never used by customer routing.

The caller injects a Model explicitly. Tests use an offline Model; no implicit
OpenAI provider, session storage, tracing export or customer action is installed.
"""
from app.configuration.runtime import policy, message


def build_readonly_agent(model):
    from agents import Agent, function_tool
    from app.persona.store_knowledge import fetch_institutional_knowledge

    @function_tool
    def lookup_policy(question: str) -> str:
        """Return published institutional documents relevant to the question."""
        import json
        return json.dumps({'documents':fetch_institutional_knowledge(question).items}, ensure_ascii=False)

    return Agent(name='institutional_readonly', model=model,
        instructions=message('sdk_auditor_instructions'), tools=[lookup_policy],
        tool_use_behavior='stop_on_first_tool')


async def run_readonly_pilot(question, *, model):
    if policy('agentsSdkPilotEnabled') is not True:
        raise ValueError('agents_sdk_pilot_disabled')
    from agents import Runner, RunConfig
    agent = build_readonly_agent(model)
    return await Runner.run(agent, input=question, max_turns=2,
                           run_config=RunConfig(tracing_disabled=True))
