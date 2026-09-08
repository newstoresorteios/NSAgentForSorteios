"""Assemble the FastAPI app from extracted routers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.commerce.pix_webhook_api import router as pix_payments_router
from app.http.admin import router as admin_router
from app.http.brevo_webhook import router as brevo_router
from app.http.cron import router as cron_router
from app.http.debug import router as debug_router
from app.http.health import admin_router as admin_health_router
from app.http.health import router as health_router
from app.http.meta_webhook import router as meta_router
from app.http.version import AGENT_VERSION
from app.ops.conversation_lock import release_conversation_lock as _release_conversation_lock
from app.ops.observability import log_event, log_exception
from app.ops.runtime_context import reset_current_turn, set_current_turn
from app.ops.turn_runtime import LLMCallBudget, TurnRuntimeContext
from app.persona.persona_admin_api import router as persona_admin_router
from app.stories.story_admin_api import router as story_admin_router
from app.config import get_settings as _get_settings
from app.http.bindings import resolve


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    from app.db import ensure_catalog_pg_trgm

    ensure_catalog_pg_trgm()
    yield


def _request_trace_id(request: Request) -> str:
    supplied = (request.headers.get("x-request-id") or "").strip()
    if supplied and len(supplied) <= 64 and all(
        char.isalnum() or char in {"-", "_"} for char in supplied
    ):
        return supplied
    return uuid4().hex


def create_app() -> FastAPI:
    application = FastAPI(
        title="NewStoreAgent Webhook",
        version=AGENT_VERSION,
        lifespan=app_lifespan,
    )
    application.include_router(persona_admin_router)
    application.include_router(pix_payments_router)
    application.include_router(health_router)
    application.include_router(admin_health_router)
    application.include_router(admin_router)
    application.include_router(brevo_router)
    application.include_router(meta_router)
    application.include_router(cron_router)
    application.include_router(story_admin_router)
    application.include_router(debug_router)

    @application.middleware("http")
    async def turn_runtime_middleware(request: Request, call_next):
        get_settings = resolve("get_settings", _get_settings)
        release_conversation_lock = resolve(
            "release_conversation_lock", _release_conversation_lock
        )
        settings = get_settings()
        path = request.url.path
        monitored_path = path.startswith(("/api/webhooks/", "/api/test/agent"))
        http_obs = bool(getattr(settings, "agent_http_obs_logs", False))
        runtime_enabled = bool(getattr(settings, "agent_runtime_enabled", True))

        if not runtime_enabled and not http_obs:
            return await call_next(request)

        attach_turn = runtime_enabled and (monitored_path or http_obs)
        if not attach_turn:
            return await call_next(request)

        from app.llm.llm_call_policy import build_llm_call_budget

        budget_cfg = build_llm_call_budget(execution_path="normal")
        context = TurnRuntimeContext(
            trace_id=_request_trace_id(request),
            llm_budget=LLMCallBudget(
                max_calls=int(budget_cfg.get("max_calls") or 2),
                enforce=bool(budget_cfg.get("enforce", True)),
            ),
        )
        context.execution_path = str(budget_cfg.get("execution_path") or "normal")
        context.start_stage("request")
        token = set_current_turn(context)
        status_code = 500
        try:
            raw_query = str(request.url.query or "")
            safe_query = None
            if raw_query:
                import re as _re

                safe_query = _re.sub(
                    r"(?i)(token|secret|api_key|apikey|authorization)=([^&]*)",
                    r"\1=[REDACTED]",
                    raw_query,
                )[:200]
            log_event(
                "http.request",
                {
                    "method": request.method,
                    "path": path,
                    "query": safe_query,
                    "content_type": request.headers.get("content-type"),
                    "user_agent": (request.headers.get("user-agent") or "")[:120] or None,
                    "monitored_path": monitored_path,
                },
            )
            response = await call_next(request)
            status_code = getattr(response, "status_code", 200) or 200
            response.headers["X-Trace-ID"] = context.trace_id
            return response
        except Exception as exc:
            if not isinstance(exc, HTTPException):
                log_exception(
                    "http.exception",
                    exc,
                    {"method": request.method, "path": path},
                )
            raise
        finally:
            lock_handle = getattr(request.state, "conversation_lock_handle", None)
            await release_conversation_lock(lock_handle)
            drain_keys = getattr(request.state, "inbox_drain_keys", None)
            if lock_handle is not None and drain_keys:
                try:
                    from app.ingress.busy import drain_lock_deferred_inbound

                    await drain_lock_deferred_inbound(
                        conversation_keys=list(drain_keys),
                        database_url=getattr(get_settings(), "database_url", ""),
                    )
                except Exception as exc:
                    log_exception(
                        "agent.lock.deferred_drain_failed",
                        exc,
                        {"drain_key_count": len(list(drain_keys))},
                    )
            context.finish_stage("request")
            summary = context.safe_summary()
            log_event(
                "http.response",
                {
                    "method": request.method,
                    "path": path,
                    "status_code": status_code,
                    "latency_ms": context.stage_durations_ms.get("request"),
                },
            )
            if monitored_path:
                print("[agent.runtime]", summary)
                log_event("runtime.summary", summary)
            reset_current_turn(token)

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        log_exception(
            "app.unhandled_exception",
            exc,
            {
                "method": request.method,
                "path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "internal_server_error"},
        )

    return application
