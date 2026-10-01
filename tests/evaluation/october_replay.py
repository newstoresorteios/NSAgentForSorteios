"""Offline, sequential execution of the October incident inputs.

The production pipeline, context evolution and tool policies run unchanged.
This session supplies intent annotations, while tools use a labelled synthetic
catalog. Other model boundaries are unavailable, never an expected-answer stub.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from tests.evaluation.october_annotations import ANNOTATIONS, get_annotation


ROOT = Path(__file__).resolve().parents[2]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def local_adapter_revision():
    adapter = ROOT.parent / 'TRAYadaptor'
    try:
        result = subprocess.run(['git', '-c', 'safe.directory=' + adapter.as_posix(), '-C', str(adapter),
                                 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True, timeout=3)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 'unavailable'


def load_runtime(snapshot: Path, workspace: str):
    from app.persona.persona_models import PersonaVersion
    from app.persona.persona_runtime import build_persona_runtime, apply_policy_overrides

    agents = [r for r in read_json(snapshot / "workspace_agents.json")
              if r["workspace_id"] == workspace and r["agent_type"] == "nsagent"]
    if not agents:
        return None
    version = max(r["config_version"] for r in agents)
    config = next(r for r in read_json(snapshot / "config_versions.json")
                  if r["workspace_id"] == workspace and r["version"] == version)
    definitions = {r["key"]: r["definition"] for r in read_json(snapshot / "catalog_configuration.json")}
    # Candidate fields introduced after the snapshot have their explicit defaults.
    for field in read_json(ROOT / "sql/seeds/operator_catalog.json"):
        definitions.setdefault(field["key"], field)
    fields = list(definitions.values())
    values = {field["key"]: deepcopy(field.get("default")) for field in fields}
    values.update(deepcopy(config["configuration"]["runtime"]["values"]))
    active = max((r for r in read_json(snapshot / "personas.json")
                  if r["workspace_id"] == workspace and r["status"] == "active" and r["tenant_id"] == "newstore"),
                 key=lambda r: r["version"])
    active = PersonaVersion(**{**active, "instructions_hash": digest(active["instructions"])})
    profile_id = active.metadata.get("chatboPersonaId")
    profiles = [r for r in read_json(snapshot / "persona_panel.json")
                if r["workspace_id"] == workspace and r["persona_id"] == profile_id]
    profile = max(profiles, key=lambda r: r["version"])["snapshot"] if profiles else None
    persona = build_persona_runtime(active=active, chatbo_profile=profile)
    persona.workspace_id = workspace
    persona.configuration_bundle = {"version": version, "fields": fields, "values": values}
    persona.runtime_configuration = deepcopy(values)
    persona = apply_policy_overrides(persona, values, source="offline_snapshot")
    return persona


def synthetic_commerce():
    products = []
    for pid, brand, name, reference, price, availability in (
        (91001, "Seiko", "Presage Cocktail Midnight Mockingbird Automático Verde", "SSA459J1", 2100, "Disponível em 30 dias úteis"),
        (91002, "Certina", "DS Action Day Date Verde", "C032.430.11.091.00", 3100, "Disponível em 30 dias úteis"),
        (91003, "Traska", "Commuter Aço Azul", "OFFLINE-TRASKA", 2500, "Pronta entrega"),
    ):
        products.append({"id": str(pid), "name": f"Relógio {brand} {name} {reference}", "reference": reference,
            "brand": brand, "current_price": price, "price": price, "stock": 3, "available": True,
            "availability": availability, "available_in_store": availability == "Pronta entrega",
            "order_days_availability": 0 if availability == "Pronta entrega" else 30,
            "available_for_purchase": True, "category_id": "1", "upon_request": False,
            "url": f"https://www.newstorerj.com.br/offline-fixture-{pid}",
            "product_url": f"https://www.newstorerj.com.br/offline-fixture-{pid}",
            "description": "Movimento automático com corda manual. Mostrador verde. Pulseira de aço.",
            "characteristics": {"Movimento": ["Automático", "Corda manual"], "Cor": "Verde"}})
    customer = {"id": "5001", "cpf": "52998224725", "name": "Cliente sintético", "phone": "5511999900000"}
    orders = [{"id": order_id, "order_id": order_id, "customer_id": "5001", "status": "EM PREPARAÇÃO",
               "status_group": "awaiting_shipment", "payment_status": "paid", "has_payment": True,
               "payment": {"method": "Pix", "type": "pix", "has_payment": True, "payment_date": "2026-09-07"},
               "total": 3100, "estimated_delivery_date": "2026-09-16", "shipment_date": None,
               "customer": deepcopy(customer), "products": [deepcopy(products[1])]} for order_id in ("26052", "25696")]
    return {"products": products, "customers": [customer], "orders": orders,
            "categories": [{"id": "1", "name": "Relógios", "parent_id": ""}]}


async def run_offline_turn(conversation, step, persona, history, state, simulator, monkeypatch):
    from app.config import get_settings
    from app.evaluation.context import EvaluationContext, bind_evaluation, reset_evaluation
    from app.llm.turn_understanding import TurnUnderstanding, turn_understanding_to_sales
    from app.message_pipeline import process_incoming_message
    from app.models import IncomingMessage, SalesInterpretation
    from app.ops.runtime_context import reset_current_turn, set_current_turn
    from app.ops.turn_runtime import LLMCallBudget, TurnRuntimeContext

    key = conversation["key"]
    cid = "offline-october:" + key
    history = [{**turn, "conversation_id": cid} for turn in history]
    state = {**deepcopy(state), "last_conversation_id": cid}
    context = EvaluationContext(persona.workspace_id, persona, history=history, state=state, simulator=simulator)
    annotations_used = []
    unavailable = []
    from app.configuration.runtime import settings_from_bundle
    cfg = settings_from_bundle(get_settings(), persona.configuration_bundle)
    runtime = TurnRuntimeContext(trace_id=cid + ":" + step["case_id"], llm_budget=LLMCallBudget(
        max_calls=cfg.agent_max_llm_calls_per_turn, enforce=cfg.agent_llm_budget_enabled))

    async def annotated_parse(**kwargs):
        schema = kwargs.get("text_format")
        if schema in {TurnUnderstanding, SalesInterpretation}:
            runtime.register_openai_call("decision")
            annotations_used.append(step["case_id"])
            parsed = TurnUnderstanding.model_validate(get_annotation(step["case_id"]))
            return SimpleNamespace(parsed=parsed if schema is TurnUnderstanding else turn_understanding_to_sales(parsed))
        unavailable.append("structured:" + getattr(schema, "__name__", "unknown"))
        raise ValueError("offline_model_boundary_unavailable")

    async def no_generated_prose(**kwargs):
        unavailable.append("text:" + str(kwargs.get("call_type") or "unknown"))
        raise ValueError("offline_model_boundary_unavailable")

    async def ready_delivery(_client, query):
        # Frozen tool facts, not the agent's expected response. Public availability
        # remains distinct from physical stock in the unchanged product code.
        context.tool_calls.append({"name": "search_ready_delivery", "args": {"query": query}, "simulated": True})
        products = list(simulator.products.values())
        mentioned = [brand for brand in ("seiko", "certina", "traska") if brand in query.casefold()]
        if mentioned:
            products = [p for p in products if p["brand"].casefold() in mentioned]
        return {"success": True, "complete": True, "requiresModel": False, "checkedAt": step["recorded_at"],
                "products": [{"name": p["name"], "reference": p["reference"], "listedAvailable": True,
                              "url": f"https://www.newstorerj.com/offline-fixture-{p['id']}"} for p in products]}

    monkeypatch.setattr("app.llm.openai_gateway.parse_structured_output", annotated_parse)
    monkeypatch.setattr("app.llm.openai_gateway.generate_text_output", no_generated_prose)
    monkeypatch.setattr("app.tray.tray_adapter_client.TrayAdapterClient.search_ready_delivery", ready_delivery)
    eval_token = bind_evaluation(context)
    runtime_token = set_current_turn(runtime)
    result = None
    error = None
    try:
        incoming = IncomingMessage(text=step["input"], channel=conversation["channel"], conversation_id=cid,
            sender_key="offline:" + key, sender_phone="5511999900000" if conversation["channel"] == "whatsapp" else None,
            raw={"inbound_id": int(step["case_id"]), "timestamp": step["recorded_at"], "message_sent_at": step["recorded_at"]})
        result = await asyncio.wait_for(process_incoming_message(incoming, {"found": False}), timeout=15)
    except Exception as exc:
        error = type(exc).__name__
    finally:
        reset_current_turn(runtime_token)
        reset_evaluation(eval_token)
    return {
        "case_id": step["case_id"], "conversation": key, "split": conversation["split"],
        "reply": result.reply_text if result else "", "intent": result.intent if result else None,
        "safety_reason": result.safety_reason if result else None,
        "handoff_required": bool(result and result.handoff_required),
        "metadata": result.response_metadata if result else {}, "error": error,
        "annotated_interpretations_used": len(annotations_used), "unavailable_model_boundaries": unavailable,
        "tools": context.tool_calls, "blocked": context.blocked, "runtime": runtime.safe_summary(),
        "next_state": context.state, "input_state_hash": digest(state), "history_hash": digest(history),
        "real_model_calls": 0, "remote_generation_exercised": False,
        "candidate_outcome": "pending_session_semantic_review",
    }


async def run_corpus(corpus_path: Path, snapshot: Path, output: Path):
    from app.config import get_settings
    from app.evaluation.simulator import CommerceSimulator
    from app.evaluation.version_manifest import build_version_manifest
    from app.configuration.runtime import settings_from_bundle

    corpus = read_json(corpus_path)
    ids = {step["case_id"] for conv in corpus["conversations"] for step in conv["steps"]}
    if ids != set(ANNOTATIONS):
        raise ValueError("annotation_coverage_mismatch")
    threads = {digest([row["workspace_id"], row["channel"], row["conversation_id"]])[:16]: row
               for row in read_json(snapshot / "today_threads.json")}
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    manifests = {}
    blocked_network_attempts = []
    source_hashes = {name: hashlib.sha256((snapshot / name).read_bytes()).hexdigest() for name in (
        "config_versions.json", "catalog_configuration.json", "personas.json", "persona_panel.json", "workspace_agents.json")}
    with pytest.MonkeyPatch.context() as patch:
        for name, value in {"OPENAI_API_KEY": "offline-annotation-only", "DATABASE_URL": "", "DRY_RUN": "true",
                            "AUTO_CREATE_TABLES": "false", "TRAY_ADAPTER_URL": "https://offline.invalid",
                            "TRAY_ADAPTER_TOKEN": "offline-fixture", "AGENT_DB_PERSONA_ENABLED": "false"}.items():
            patch.setenv(name, value)
        get_settings.cache_clear()

        def network_forbidden(*_args, **_kwargs):
            blocked_network_attempts.append("sync_network_blocked")
            raise RuntimeError("offline_network_forbidden")

        async def async_network_forbidden(*_args, **_kwargs):
            blocked_network_attempts.append("async_network_blocked")
            raise RuntimeError("offline_network_forbidden")

        patch.setattr("httpx.Client.request", network_forbidden)
        patch.setattr("httpx.AsyncClient.request", async_network_forbidden)
        patch.setattr("socket.create_connection", network_forbidden)
        for conversation in corpus["conversations"]:
            workspace = threads[conversation["key"]]["workspace_id"]
            persona = load_runtime(snapshot, workspace)
            if persona is None:
                reports.extend({"case_id": step["case_id"], "conversation": conversation["key"],
                    "split": conversation["split"], "candidate_outcome": "inconclusive",
                    "not_executed_reason": "different_agent_runtime_not_nsagent", "real_model_calls": 0,
                    "remote_generation_exercised": False, "reply": "", "next_state": {}}
                    for step in conversation["steps"])
                continue
            cfg = settings_from_bundle(get_settings(), persona.configuration_bundle)
            manifest = build_version_manifest(persona_version=persona.persona_version_id,
                bundle=persona.configuration_bundle, model="codex-session-intent-annotations",
                judge_model="unavailable-offline", case_hash=digest(conversation), mode="offline_annotated_pipeline",
                persona_content=persona.flow_params_dict(), adapter_revision=local_adapter_revision(),
                catalog_snapshot=synthetic_commerce(), extra={"snapshot_sources": source_hashes,
                    "remote_model_configured": cfg.openai_model, "annotations_hash": digest(ANNOTATIONS),
                    "remote_generation_exercised": False, "adapter_execution": "simulated_only"})
            manifests[conversation["key"]] = manifest
            simulator = CommerceSimulator(synthetic_commerce())
            history = deepcopy(conversation.get("history") or [])
            state = {}
            for step in conversation["steps"]:
                if step.get("requires_media_annotation") or step["case_id"] in {"1130", "1167", "1187"}:
                    missing = "human_takeover_requires_ingress_replay" if step["case_id"] == "1187" else "media_or_context_requires_separate_replay"
                    report = {"case_id": step["case_id"], "conversation": conversation["key"], "split": conversation["split"],
                              "candidate_outcome": "inconclusive", "not_executed_reason": missing,
                              "real_model_calls": 0, "remote_generation_exercised": False, "reply": "", "next_state": deepcopy(state)}
                else:
                    report = await run_offline_turn(conversation, step, persona, history, state, simulator, patch)
                    state = deepcopy(report["next_state"])
                report["version_manifest"] = manifest
                report["initial_state_limit"] = "Prior state missing in audit export; candidate state chained from an empty initial state."
                reports.append(report)
                history.append({"role": "user", "content": step["input"]})
                if report["reply"]:
                    history.append({"role": "assistant", "content": report["reply"], "metadata": report.get("metadata") or {}})
        get_settings.cache_clear()
    summary = {"method": "Production pipeline with session-annotated intent, synthetic commerce and no model-generated prose stubs.",
               "corpus_hash": digest(corpus), "turn_count": len(reports), "conversation_count": len(corpus["conversations"]),
               "executed": sum("not_executed_reason" not in r for r in reports),
               "not_executed": dict(Counter(r["not_executed_reason"] for r in reports if "not_executed_reason" in r)),
               "execution_errors": dict(Counter(r["error"] for r in reports if r.get("error"))),
               "real_model_calls": 0, "remote_generation_exercised": False, "automatic_promotion": False,
               "blocked_network_attempts": dict(Counter(blocked_network_attempts)),
               "semantic_review": "pending", "source_hashes": source_hashes, "reports": reports}
    (output / "observations.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return summary
