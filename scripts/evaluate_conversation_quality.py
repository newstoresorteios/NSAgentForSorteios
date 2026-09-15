"""Real-model replay with the published persona and isolated catalog fixtures.

Database connections are read-only; this script never calls a channel sender,
cart/order endpoint or production catalog. New migration defaults are overlaid
in memory so evaluation can run before deployment. --via-vercel uses temporary
OIDC authentication and the OpenAI-compatible Gateway, only for this evaluation.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from dotenv import dotenv_values
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ASK = "tem relogio automatico e com cristal de safira até 2500 reais?"
# Captured identities/specifications, with synthetic stock for controlled replay.
# The positive option is explicitly synthetic, not a claim about the store.
PRODUCTS = [
    {"id":"13434","name":"Seiko Spirit SBTR019","description":"Movimento de quartzo, cristal Hardlex","price":2499.99,"available":True,"stock":2},
    {"id":"893","name":"Citizen Promaster BN0151-09L","description":"Eco-Drive solar, cristal mineral","price":2499.99,"available":True,"stock":2},
    {"id":"4917","name":"Orient RA-AA0010B19B","description":"Especificações não informadas","price":2399.99,"available":True,"stock":2},
]
POSITIVE = {"id":"eval-confirmed","name":"Relógio Avaliação Automático Safira","reference":"EVAL-001",
    "description":"Movimento automático; cristal de safira","price":2200,"available":True,"stock":2}


async def evaluate(persona, args):
    from app.catalog.retrieval.technical import retrieve_technical_products
    from app.catalog.specs.requirements import normalize_requirements
    from app.commerce.commerce_context import CommerceConversationState, resolve_commerce_reference
    from app.configuration.runtime import bind_bundle, reset_bundle, settings_from_bundle
    from app.config import get_settings
    from app.llm.llm_call_policy import prepare_catalog_budget, resolve_turn_llm_budget
    from app.models import IncomingMessage
    from app.ops.runtime_context import set_current_turn, reset_current_turn
    from app.ops.turn_runtime import TurnRuntimeContext, LLMCallBudget
    from app.persona.persona_runtime import set_persona_runtime, reset_persona_runtime
    from app.sales_agent import interpret_message, _hydrate_sales_interpretation
    from app.sales.interpreter import interpretation_to_plan
    from app.sales.responder import sales_response_with_openai
    from app.verify.response_critique import apply_response_critique_loop
    from app.verify.final_response import finalize_response
    from app.message_pipeline import compose_outbound_reply

    bundle = deepcopy(persona.configuration_bundle)
    fields = json.loads((ROOT / "sql/seeds/operator_catalog.json").read_text(encoding="utf-8"))
    existing = {f["key"] for f in bundle["fields"]}
    additions = [f for f in fields if f["key"] not in existing]
    bundle["fields"].extend(additions)
    for field in additions:
        bundle["values"][field["key"]] = field["default"]
    settings = settings_from_bundle(get_settings(), bundle)
    model = settings.openai_model
    persona.configuration_bundle = bundle
    persona.runtime_configuration = bundle["values"]
    binding, persona_token = bind_bundle(bundle, settings), set_persona_runtime(persona)
    reports = []
    try:
        for scenario in ("incident_unconfirmed", "confirmed_fixture"):
            rows = deepcopy(PRODUCTS + ([POSITIVE] if scenario == "confirmed_fixture" else []))
            calls = []
            async def tool(name, arguments):
                calls.append({"tool":name, "arguments":arguments})
                if name == "get_product":
                    return deepcopy(next(p for p in rows if p["id"] == arguments["product_id"]))
                if name == "search_products":
                    return {"products":deepcopy(rows)}
                raise AssertionError("Evaluation prohibits this tool: " + name)
            async def search(arguments):
                return await tool("search_products", arguments)

            state = CommerceConversationState(active_domain="commerce")
            incoming = IncomingMessage(text=ASK, channel="whatsapp")
            history = [{"role":"user","content":"olá"},{"role":"assistant","content":"Olá! Como posso ajudar?"}]
            runtime = TurnRuntimeContext(trace_id="eval:"+scenario, llm_budget=LLMCallBudget(**resolve_turn_llm_budget(complex_turn=False)))
            runtime_token = set_current_turn(runtime)
            started = time.perf_counter()
            try:
                interpretation = await interpret_message(incoming, recent_turns=history, commerce_state=state)
                assert interpretation._source == "openai", "The interpreter must use the real model in this evaluation"
                interpretation = _hydrate_sales_interpretation(interpretation,incoming,history,state)
                required = normalize_requirements(interpretation,ASK)
                assert required == {"mechanism":"automatic","crystal":"sapphire"}, required
                assert interpretation.preferences.budget_max == 2500
                prepare_catalog_budget(interpretation)
                session = SimpleNamespace(interpretation=interpretation,candidates=rows,excluded_ids=set(),
                    retrieval_plan=SimpleNamespace(mode="recommendation",candidate_limit=20),
                    list_query_extras=lambda _: {}, search_products=search, execute_tool=tool)
                result = await retrieve_technical_products(session)
                result.response_metadata.update(interpretation=interpretation.model_dump(mode="json"),used_tray=True)
                generated = await sales_response_with_openai(incoming,interpretation_to_plan(interpretation,ASK),result,interpretation,state,history)
                assert generated is not None, "Generative responder did not produce a response"
                result, critique = await apply_response_critique_loop(incoming=incoming,result=generated,
                    recent_turns=history,commerce_state=state,mode=settings.agent_critique_mode,
                    max_retries=settings.agent_critique_max_retries,execute=tool)
                result = compose_outbound_reply(incoming,result,max_reply_chars=settings.max_reply_chars)
                result, final_state = finalize_response(result,incoming=incoming,interpretation=interpretation,previous_state=state)
                report = {"scenario":scenario,"reply":result.reply_text,"handoff":result.handoff_required,
                    "criteria":required,"budget":interpretation.preferences.budget_max,
                    "review":critique.model_dump(mode="json"),"final_validation":result.response_metadata.get("final_response_validation"),
                    "calls":calls,"runtime":runtime.safe_summary(),"elapsed_ms":round((time.perf_counter()-started)*1000,2)}
                assert not result.handoff_required, report
                expected = ["eval-confirmed"] if scenario == "confirmed_fixture" else []
                assert [p.product_id for p in final_state.last_presented_products] == expected, report
                assert runtime.llm_calls_by_type.get("decision") and any(k.startswith("response_composition") for k in runtime.llm_calls_by_type), report
            finally:
                reset_current_turn(runtime_token)

            follow_text = "o primeiro também é automático?" if expected else "quero o primeiro"
            history += [{"role":"user","content":ASK},{"role":"assistant","content":result.reply_text}]
            follow_runtime = TurnRuntimeContext(trace_id="eval:followup",llm_budget=LLMCallBudget(**resolve_turn_llm_budget(complex_turn=False)))
            token = set_current_turn(follow_runtime)
            try:
                follow = IncomingMessage(text=follow_text,channel="whatsapp")
                next_interpretation = await interpret_message(follow,recent_turns=history,commerce_state=final_state)
                next_interpretation = _hydrate_sales_interpretation(next_interpretation,follow,history,final_state)
                selected, source = resolve_commerce_reference(next_interpretation,final_state)
                assert (selected.product_id if selected else None) == (expected[0] if expected else None)
                assert next_interpretation.preferences.mechanism == "automatic"
                assert next_interpretation.preferences.crystal == "sapphire"
                report["followup"] = {"text":follow_text,"selected_product_id":selected.product_id if selected else None,
                    "source":source,"preferences":next_interpretation.preferences.model_dump(mode="json"),"runtime":follow_runtime.safe_summary()}
                reports.append(report)
            finally:
                reset_current_turn(token)
    finally:
        reset_persona_runtime(persona_token)
        reset_bundle(binding)
    return {"passed":True,"model":model,"transport":"vercel_gateway" if args.via_vercel else "openai",
            "persona_version":persona.persona_version_id,"configuration_version":bundle["version"],
            "migration_defaults_overlaid":len(additions),"catalog":"isolated fixtures","production_writes":0,"scenarios":reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,required=True)
    parser.add_argument("--db-env-file",type=Path,required=True)
    parser.add_argument("--workspace",required=True)
    parser.add_argument("--via-vercel",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    env = dotenv_values(args.env_file)
    for key,value in env.items():
        if value and value != "[SENSITIVE]":
            os.environ[key] = value
    key = env.get("VERCEL_OIDC_TOKEN") if args.via_vercel else env.get("OPENAI_API_KEY")
    if not key or key == "[SENSITIVE]":
        raise SystemExit("A usable local model credential is required; secret values are never printed.")
    os.environ["OPENAI_API_KEY"] = key
    os.environ["AGENT_DB_PERSONA_ENABLED"] = "true"
    db_env = dotenv_values(args.db_env_file)
    dsn = db_env.get("DATABASE_URL") or db_env.get("SUPABASE_DB_URL")
    @contextmanager
    def readonly_conn():
        with psycopg.connect(dsn,row_factory=dict_row,connect_timeout=10) as conn:
            conn.read_only = True
            yield conn
    with ExitStack() as stack:
        stack.enter_context(patch("app.db.get_conn",readonly_conn))
        from app.persona.persona_runtime import load_persona_runtime
        persona = load_persona_runtime(workspace_id=args.workspace)
        assert persona.enabled and persona.configuration_bundle["version"] > 0
        stack.enter_context(patch("app.catalog.index.catalog_index.index_products_best_effort",lambda *a,**k:None))
        if args.via_vercel:
            from openai import AsyncOpenAI
            from app.llm.openai_gateway import ChatCompletionsGateway
            client = AsyncOpenAI(api_key=key,base_url="https://ai-gateway.vercel.sh/v1",max_retries=0,timeout=45)
            # Keep model capability decisions on the original published ID;
            # add the Gateway provider prefix only at the transport boundary.
            class Completions:
                async def parse(self, **kwargs):
                    kwargs["model"] = "openai/" + kwargs["model"].removeprefix("openai/")
                    return await client.chat.completions.parse(**kwargs)
                async def create(self, **kwargs):
                    kwargs["model"] = "openai/" + kwargs["model"].removeprefix("openai/")
                    return await client.chat.completions.create(**kwargs)
            proxy = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
            stack.enter_context(patch("app.llm.openai_gateway.get_openai_gateway",lambda:ChatCompletionsGateway(proxy)))
        try:
            if args.via_vercel:
                # Fail once on authentication/billing before invoking model fallbacks.
                import httpx
                model = persona.runtime_configuration["openai_model"]
                preflight = httpx.post("https://ai-gateway.vercel.sh/v1/chat/completions",
                    headers={"Authorization":"Bearer " + key},
                    json={"model":"openai/"+model.removeprefix("openai/"),"messages":[{"role":"user","content":"Responda OK."}],"max_completion_tokens":30},timeout=30)
                if not preflight.is_success:
                    error = preflight.json().get("error") or {}
                    raise RuntimeError("real_model_evaluation_blocked:"+str(error.get("type") or preflight.status_code))
            report = asyncio.run(evaluate(persona,args))
        except Exception as exc:
            args.output.write_text(json.dumps({"passed":False,"error_type":type(exc).__name__,"detail":str(exc)[:500]},ensure_ascii=False,indent=2),encoding="utf-8")
            raise
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")
        print(json.dumps({"passed":report["passed"],"scenarios":len(report["scenarios"]),"model":report["model"]}))


if __name__ == "__main__":
    main()
