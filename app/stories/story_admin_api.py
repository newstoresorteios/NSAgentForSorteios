"""Instagram Story admin CRUD — tenant from header/persona first."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.security import verify_admin_token

router = APIRouter(prefix="/api/admin/instagram", tags=["admin-stories"])


def resolve_story_tenant(
    request: Request,
    settings,
    *,
    query_tenant: str | None = None,
    body: dict[str, Any] | None = None,
) -> str:
    return str(
        request.headers.get("x-tenant-id")
        or getattr(settings, "agent_persona_tenant_id", "")
        or (query_tenant or "")
        or ((body or {}).get("tenant_id") if isinstance(body, dict) else "")
        or ""
    ).strip()


@router.get("/stories/health", dependencies=[Depends(verify_admin_token)])
async def admin_instagram_story_health():
    settings = get_settings()
    real_ok = bool(getattr(settings, "instagram_story_real_payload_validated", False))
    mode = str(getattr(settings, "instagram_story_rollout_mode", "off") or "off")
    canary_or_full_allowed = real_ok or mode in {"off", "diagnostics", "shadow"}
    return {
        "ok": True,
        "recognition_enabled": bool(
            getattr(settings, "instagram_story_recognition_enabled", False)
        ),
        "rollout_mode": mode,
        "real_payload_validated": real_ok,
        "video_frame_analysis_enabled": bool(
            getattr(settings, "instagram_story_video_frame_analysis_enabled", False)
        ),
        "canary_full_gate_ok": canary_or_full_allowed,
        "note": (
            "Set INSTAGRAM_STORY_REAL_PAYLOAD_VALIDATED=true only after a real "
            "sanitized Brevo Story payload is covered by tests. "
            "If a past ZIP exposed VERCEL_OIDC_TOKEN, revoke it in Vercel."
        ),
    }


@router.get("/stories", dependencies=[Depends(verify_admin_token)])
async def admin_list_instagram_stories(
    request: Request,
    tenant_id: str | None = None,
    status: str | None = None,
    instagram_account_id: str | None = None,
    product_id: str | None = None,
    limit: int = 50,
):
    from app.identity.request_principal import principal_from_admin_token
    from app.stories.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    resolved_tenant = resolve_story_tenant(request, settings, query_tenant=tenant_id)
    if not resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_required"}, status_code=400)
    principal = principal_from_admin_token(
        subject_id=str(
            request.headers.get("x-admin-actor")
            or request.headers.get("x-admin-id")
            or "admin_token"
        ),
        tenant_ids=[resolved_tenant, "*"],
    )
    try:
        principal.require_tenant(resolved_tenant)
    except PermissionError:
        return JSONResponse({"ok": False, "error": "tenant_forbidden"}, status_code=403)
    rows = StoryProductRepository().list_stories(
        tenant_id=resolved_tenant,
        status=status,
        instagram_account_id=instagram_account_id,
        product_id=product_id,
        limit=limit,
    )
    return {
        "ok": True,
        "tenant_id": resolved_tenant,
        "items": [row.model_dump(mode="json") for row in rows],
    }


@router.get("/stories/{row_id}", dependencies=[Depends(verify_admin_token)])
async def admin_get_instagram_story(row_id: int, request: Request, tenant_id: str | None = None):
    from app.stories.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    resolved_tenant = resolve_story_tenant(request, settings, query_tenant=tenant_id)
    if not resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_required"}, status_code=400)
    row = StoryProductRepository().get_by_id(tenant_id=resolved_tenant, row_id=row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return {"ok": True, "item": row.model_dump(mode="json")}


@router.post("/stories/link-product", dependencies=[Depends(verify_admin_token)])
async def admin_link_instagram_story_product(request: Request):
    from app.stories.story_publication_link_service import (
        register_published_story,
        validate_link_payload,
    )

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    actor = str(
        request.headers.get("x-admin-actor")
        or request.headers.get("x-admin-id")
        or "admin_token"
    ).strip()[:120]
    try:
        cleaned = validate_link_payload(body)
        cleaned["confirmed_by"] = actor
        assoc = register_published_story(**cleaned)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": type(exc).__name__},
            status_code=500,
        )
    return {"ok": True, "item": assoc.model_dump(mode="json")}


@router.post("/stories/{row_id}/confirm", dependencies=[Depends(verify_admin_token)])
async def admin_confirm_instagram_story(row_id: int, request: Request):
    from app.verify.fact_authority import catalog_item_key_for
    from app.catalog.index.repository import CatalogIndexRepository
    from app.identity.request_principal import principal_from_admin_token
    from app.stories.story_product_repository import StoryProductRepository
    from app.ops.observability import log_event
    from app.db import ensure_tables, get_conn

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    resolved_tenant = resolve_story_tenant(request, settings, body=body)
    if not resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_required"}, status_code=400)
    actor = str(
        request.headers.get("x-admin-actor")
        or request.headers.get("x-admin-id")
        or "admin_token"
    ).strip()[:120]
    principal = principal_from_admin_token(
        subject_id=actor,
        tenant_ids=[resolved_tenant, "*"],
    )
    try:
        principal.require_tenant(resolved_tenant)
    except PermissionError:
        return JSONResponse({"ok": False, "error": "tenant_forbidden"}, status_code=403)
    repo = StoryProductRepository()
    existing = repo.get_by_id(tenant_id=resolved_tenant, row_id=row_id)
    if existing is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    product_id = str(body.get("product_id") or "").strip()
    if not product_id:
        return JSONResponse({"ok": False, "error": "product_required"}, status_code=400)
    variant_id = (
        str(body["variant_id"]).strip()
        if body.get("variant_id") not in (None, "")
        else None
    )
    catalog_item_key = catalog_item_key_for(product_id, variant_id)
    index_row = CatalogIndexRepository().get_by_product_and_variant(
        tenant_id=resolved_tenant,
        product_id=product_id,
        variant_id=variant_id,
    )
    if index_row is not None and str(index_row.get("tenant_id") or "") != resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_mismatch"}, status_code=403)
    confirmed = repo.confirm_match(
        tenant_id=resolved_tenant,
        provider=existing.provider,
        instagram_account_id=existing.instagram_account_id,
        story_media_id=existing.story_media_id,
        catalog_item_key=catalog_item_key,
        product_id=product_id,
        variant_id=variant_id,
        match_source="manual",
        match_confidence=1.0,
        match_status="manually_confirmed",
        confirmed_by=actor,
        explanation={
            "reason": str(body.get("reason") or "manual_confirm")[:200],
            "previous_product_id": existing.product_id,
            "previous_variant_id": existing.variant_id,
            "previous_catalog_item_key": existing.catalog_item_key,
            "admin_id": actor,
            "tenant_id": resolved_tenant,
            "story_row_id": row_id,
        },
    )
    try:
        ensure_tables()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.story_product_association_audit (
                        tenant_id, story_row_id, story_media_id, actor_id, action,
                        previous_product_id, previous_variant_id, previous_catalog_item_key,
                        new_product_id, new_variant_id, new_catalog_item_key, reason
                    ) VALUES (
                        %s, %s, %s, %s, 'confirm',
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        resolved_tenant,
                        row_id,
                        existing.story_media_id,
                        actor,
                        existing.product_id,
                        existing.variant_id,
                        existing.catalog_item_key,
                        product_id,
                        variant_id,
                        catalog_item_key,
                        str(body.get("reason") or "manual_confirm")[:200],
                    ),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log_event(
            "instagram_story.admin_audit_failed",
            {"error_type": type(exc).__name__},
        )
    log_event(
        "instagram_story.admin_confirm",
        {
            "tenant_id": resolved_tenant,
            "story_row_id": row_id,
            "admin_id": actor,
            "product_id": product_id,
            "variant_id": variant_id,
            "previous_product_id": existing.product_id,
        },
    )
    return {"ok": True, "item": confirmed.model_dump(mode="json") if confirmed else None}


@router.post("/stories/{row_id}/unlink", dependencies=[Depends(verify_admin_token)])
async def admin_unlink_instagram_story(row_id: int, request: Request):
    from app.stories.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = (
        await request.json()
        if request.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    resolved_tenant = resolve_story_tenant(
        request, settings, body=body if isinstance(body, dict) else None
    )
    if not resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_required"}, status_code=400)
    actor = str(
        request.headers.get("x-admin-actor")
        or request.headers.get("x-admin-id")
        or "admin_token"
    ).strip()[:120]
    row = StoryProductRepository().unlink(
        tenant_id=resolved_tenant,
        row_id=row_id,
        confirmed_by=actor,
    )
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return {"ok": True, "item": row.model_dump(mode="json")}


@router.post("/stories/{row_id}/reprocess", dependencies=[Depends(verify_admin_token)])
async def admin_reprocess_instagram_story(row_id: int, request: Request):
    from app.stories.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = (
        await request.json()
        if request.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    resolved_tenant = resolve_story_tenant(
        request, settings, body=body if isinstance(body, dict) else None
    )
    if not resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_required"}, status_code=400)
    actor = str(
        request.headers.get("x-admin-actor")
        or request.headers.get("x-admin-id")
        or "admin_token"
    ).strip()[:120]
    repo = StoryProductRepository()
    existing = repo.get_by_id(tenant_id=resolved_tenant, row_id=row_id)
    if existing is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    reset = repo.unlink(
        tenant_id=resolved_tenant,
        row_id=row_id,
        confirmed_by=f"{actor}:reprocess",
    )
    return {
        "ok": True,
        "item": reset.model_dump(mode="json") if reset else None,
        "note": "Association reset to pending; next customer reply will re-analyze once.",
    }
