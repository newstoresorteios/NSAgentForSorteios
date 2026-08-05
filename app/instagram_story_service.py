"""Operational Instagram Story product resolution (wired into the sales agent)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any

from .config import get_settings
from .fact_authority import authorize_products_for_responder
from .instagram_story_intent import (
    detect_story_question_type,
    should_route_story_question,
)
from .instagram_story_media import (
    StoryMediaError,
    download_story_media,
    extract_video_frames_best_effort,
)
from .instagram_story_models import (
    InstagramStoryContext,
    StoryConversationReference,
    StoryProductCandidate,
    StoryResolutionResult,
    StoryVisualUnderstanding,
)
from .models import AgentResult, IncomingMessage
from .observability import log_event
from .openai_routing import bucket_for_key
from .story_product_matcher import classify_match, match_story_to_catalog
from .story_product_repository import StoryProductRepository
from .story_visual_analyzer import (
    analyze_story_image,
    get_cached_visual_analysis,
    put_cached_visual_analysis,
)


def _tenant_id() -> str:
    return str(getattr(get_settings(), "agent_persona_tenant_id", None) or "newstore")


def story_rollout_allows(*, tenant_id: str, story: InstagramStoryContext) -> tuple[bool, str]:
    settings = get_settings()
    mode = str(getattr(settings, "instagram_story_rollout_mode", "off") or "off").casefold()
    if not bool(getattr(settings, "instagram_story_recognition_enabled", False)):
        return False, "recognition_disabled"
    if mode == "off":
        return False, "rollout_off"
    if mode == "full":
        return True, "full"
    if mode == "shadow":
        return True, "shadow"
    if mode == "canary":
        percent = float(getattr(settings, "instagram_story_canary_percent", 5) or 5) / 100.0
        key = f"{tenant_id}:{story.instagram_account_id}:{story.story_media_id or ''}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        # Reuse sticky bucket helper (0..9999).
        in_canary = bucket_for_key(digest) < int(round(percent * 10_000))
        return in_canary, "canary" if in_canary else "canary_excluded"
    return False, "unknown_mode"


async def _revalidate_product(
    *,
    product_id: str,
    execute_tool: Any,
    variant_id: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    try:
        result = await execute_tool("get_product", {"product_id": str(product_id)})
    except Exception:
        return None, True
    if not isinstance(result, dict) or result.get("error"):
        return None, True
    product = dict(result)
    product["id"] = str(product.get("id") or product_id)
    if variant_id is not None:
        product["variant_id"] = variant_id
    product["_revalidated"] = True
    product["_factual_source"] = "tray_live"
    return product, False


def _compose_reply(
    *,
    question_type: Any,
    product: dict[str, Any] | None,
    status: str,
    candidates: list[StoryProductCandidate],
    tray_failed: bool = False,
    expired: bool = False,
) -> str:
    if expired:
        return (
            "Não consegui mais acessar a imagem desse Story. "
            "Se você enviar um print, eu identifico o modelo e confirmo o valor."
        )
    if status == "ambiguous" and candidates:
        options = []
        for candidate in candidates[:2]:
            label = candidate.product_id
            options.append(label)
        if len(candidates) >= 2:
            return (
                "Quero confirmar o modelo para não passar o valor errado. "
                "Você está falando do primeiro ou do segundo modelo parecido desse Story?"
            )
        return (
            "Nesse Story a identificação ficou parcial. "
            "Pode me dizer a marca ou a cor do mostrador?"
        )
    if status in {"not_found", "failed"}:
        return (
            "Não consegui identificar com segurança o produto desse Story. "
            "Se puder enviar um print mais nítido ou a referência, eu confirmo."
        )
    if tray_failed and product is not None:
        name = str(product.get("name") or "o modelo")
        return (
            f"Identifiquei {name}, mas não consegui confirmar o preço atualizado agora. "
            "Posso tentar novamente em instantes ou encaminhar para o atendimento."
        )
    if product is None:
        return (
            "Recebi sua pergunta sobre o Story. "
            "Me confirma a cor ou o modelo para eu consultar o valor atualizado?"
        )
    name = str(product.get("name") or "Esse modelo")
    price = product.get("promotional_price") or product.get("price") or product.get("current_price")
    stock = product.get("stock")
    url = product.get("url")
    available = product.get("available")
    if question_type.value == "product_link" and url:
        return f"Esse é o {name}. Segue o link atualizado: {url}"
    if question_type.value == "availability":
        if available is False or (isinstance(stock, int) and stock <= 0):
            return (
                f"Esse é o {name}. Consultei agora e ele não está disponível neste momento. "
                "Quer que eu veja alternativas parecidas?"
            )
        return f"Esse é o {name}. Consultei agora e ele está disponível neste momento."
    if question_type.value == "color_options":
        return (
            f"Esse é o {name}. Posso verificar outras cores/variantes dele no catálogo. "
            "Qual cor você prefere?"
        )
    if price is not None:
        try:
            price_txt = f"R$ {float(price):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except (TypeError, ValueError):
            price_txt = str(price)
        avail_txt = "está disponível" if available is not False else "não está disponível"
        return (
            f"Esse é o {name}. Consultei agora e o valor atual é {price_txt}. "
            f"Ele {avail_txt} neste momento. Quer que eu envie o link?"
        )
    return f"Esse é o {name}. Quer que eu confirme o valor atualizado e o link?"


async def resolve_story_product_question(
    *,
    incoming: IncomingMessage,
    tenant_id: str | None = None,
    customer_context: dict[str, Any] | None = None,
    execute_tool: Any | None = None,
) -> StoryResolutionResult | None:
    _ = customer_context
    if not should_route_story_question(incoming):
        return None
    story = incoming.instagram_story
    if not isinstance(story, InstagramStoryContext):
        return None
    tenant = tenant_id or _tenant_id()
    allowed, rollout_reason = story_rollout_allows(tenant_id=tenant, story=story)
    if not allowed:
        return None

    question_type = detect_story_question_type(incoming.text)
    metrics: dict[str, Any] = {
        "story_visual_analysis_calls": 0,
        "story_rerank_calls": 0,
        "story_cache_hits": 0,
        "story_deterministic_matches": 0,
        "story_ambiguous_matches": 0,
        "rollout": rollout_reason,
    }
    shadow_only = rollout_reason == "shadow"
    media_id = str(story.story_media_id or "")
    log_event(
        "instagram_story.received",
        {
            "tenant_id": tenant,
            "media_type": story.media_type,
            "question_type": question_type.value,
            "rollout": rollout_reason,
            "story_media_id_hash": hashlib.sha256(media_id.encode()).hexdigest()[:12] if media_id else None,
        },
    )

    repo = StoryProductRepository()
    provider = story.provider or "brevo"
    account = story.instagram_account_id or "unknown"

    if not media_id:
        return StoryResolutionResult(
            resolved=False,
            match_status="failed",
            needs_clarification=True,
            failure_reason="story_media_id_missing",
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="failed",
                candidates=[],
                expired=True,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    assoc = repo.get_by_story(
        tenant_id=tenant,
        provider=provider,
        instagram_account_id=account,
        story_media_id=media_id,
    )
    if assoc is None:
        assoc = repo.create_pending(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            story_message_id=story.story_message_id,
            story_permalink=story.story_permalink,
            media_type=story.media_type,
            source_timestamp=story.source_timestamp,
            story_expires_at=story.expires_at,
        )

    if assoc and assoc.match_status in {"matched", "manually_confirmed"} and assoc.product_id:
        log_event("instagram_story.association_found", {"status": assoc.match_status})
        metrics["story_deterministic_matches"] = 1
        repo.touch_last_seen(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
        )
        product = None
        tray_failed = False
        if execute_tool is not None:
            log_event("instagram_story.revalidation_started", {"product_id": assoc.product_id})
            product, tray_failed = await _revalidate_product(
                product_id=assoc.product_id,
                execute_tool=execute_tool,
                variant_id=assoc.variant_id,
            )
            if tray_failed:
                log_event("instagram_story.revalidation_failed", {"product_id": assoc.product_id})
        evidence = []
        if product:
            authorized, grounded = authorize_products_for_responder(
                [product],
                tenant_id=tenant,
            )
            product = authorized[0] if authorized else product
            evidence = [g.model_dump(mode="json") for g in grounded]
        result = StoryResolutionResult(
            resolved=product is not None and not tray_failed,
            story_media_id=media_id,
            match_status=assoc.match_status,
            catalog_item_key=assoc.catalog_item_key,
            product_id=assoc.product_id,
            variant_id=assoc.variant_id,
            confidence=float(assoc.match_confidence or 1.0),
            factual_evidence=evidence,
            question_type=question_type,
            product_payload=product,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=product,
                status="matched",
                candidates=[],
                tray_failed=tray_failed,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )
        return result

    if assoc and assoc.match_status == "processing":
        # Soft wait — do not launch a second vision call.
        for _ in range(3):
            await asyncio.sleep(0.15)
            assoc = repo.get_by_story(
                tenant_id=tenant,
                provider=provider,
                instagram_account_id=account,
                story_media_id=media_id,
            )
            if assoc and assoc.match_status != "processing":
                break
        if assoc and assoc.match_status == "processing":
            return StoryResolutionResult(
                resolved=False,
                story_media_id=media_id,
                match_status="processing",
                needs_clarification=True,
                failure_reason="analysis_in_progress",
                question_type=question_type,
                reply_hint=(
                    "Estou confirmando o modelo desse Story. "
                    "Só um instante — ou me manda a cor/referência se preferir."
                ),
                shadow_only=shadow_only,
                metrics=metrics,
            )

    if assoc and assoc.match_status == "expired":
        return StoryResolutionResult(
            resolved=False,
            story_media_id=media_id,
            match_status="expired",
            needs_clarification=True,
            failure_reason="story_expired",
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="failed",
                candidates=[],
                expired=True,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    # Claim processing lock.
    claimed = repo.begin_processing(
        tenant_id=tenant,
        provider=provider,
        instagram_account_id=account,
        story_media_id=media_id,
    )
    if claimed is None and assoc and assoc.match_status not in {"pending", "failed", None}:
        # Another worker owns it or already finished.
        pass

    analysis: StoryVisualUnderstanding | None = None
    media_sha: str | None = None
    storage_path: str | None = None
    download_url = story.story_media_url or story.story_thumbnail_url

    if download_url:
        try:
            media = await download_story_media(download_url)
            media_sha = media.sha256
            storage_path = media.storage_path
            cached = get_cached_visual_analysis(tenant_id=tenant, media_sha256=media_sha)
            if cached is not None:
                analysis = cached
                metrics["story_cache_hits"] = 1
            else:
                if media.content_type.startswith("video/"):
                    frames = extract_video_frames_best_effort(
                        media.content,
                        max_frames=int(
                            getattr(get_settings(), "instagram_story_video_max_frames", 3)
                            or 3
                        ),
                    )
                    if not frames and story.story_thumbnail_url:
                        thumb = await download_story_media(story.story_thumbnail_url)
                        analysis = await analyze_story_image(
                            image_bytes=thumb.content,
                            content_type=thumb.content_type,
                            media_sha256=thumb.sha256,
                            media_type="image",
                        )
                        metrics["story_visual_analysis_calls"] = 1
                    elif frames:
                        analysis = await analyze_story_image(
                            image_bytes=frames[0],
                            content_type="image/jpeg",
                            media_sha256=media_sha,
                            media_type="video",
                        )
                        metrics["story_visual_analysis_calls"] = 1
                    else:
                        repo.mark_failed(
                            tenant_id=tenant,
                            provider=provider,
                            instagram_account_id=account,
                            story_media_id=media_id,
                            explanation={"reason": "video_decoder_unavailable"},
                        )
                        return StoryResolutionResult(
                            resolved=False,
                            story_media_id=media_id,
                            match_status="failed",
                            failure_reason="video_decoder_unavailable",
                            needs_clarification=True,
                            question_type=question_type,
                            reply_hint=_compose_reply(
                                question_type=question_type,
                                product=None,
                                status="failed",
                                candidates=[],
                                expired=True,
                            ),
                            shadow_only=shadow_only,
                            metrics=metrics,
                        )
                else:
                    analysis = await analyze_story_image(
                        image_bytes=media.content,
                        content_type=media.content_type,
                        media_sha256=media_sha,
                        media_type=story.media_type,
                    )
                    metrics["story_visual_analysis_calls"] = 1
                if analysis is not None and media_sha:
                    put_cached_visual_analysis(
                        tenant_id=tenant,
                        media_sha256=media_sha,
                        analysis=analysis,
                    )
            # Hash association reuse (same tenant only).
            if media_sha:
                prior = repo.find_by_media_hash(tenant_id=tenant, media_sha256=media_sha)
                if prior and prior.product_id:
                    repo.confirm_match(
                        tenant_id=tenant,
                        provider=provider,
                        instagram_account_id=account,
                        story_media_id=media_id,
                        catalog_item_key=prior.catalog_item_key or f"product:{prior.product_id}",
                        product_id=prior.product_id,
                        variant_id=prior.variant_id,
                        match_source="visual_catalog_match",
                        match_confidence=float(prior.match_confidence or 0.95),
                        explanation={"via": "media_sha256"},
                    )
                    metrics["story_deterministic_matches"] = 1
                    product = None
                    tray_failed = False
                    if execute_tool is not None:
                        product, tray_failed = await _revalidate_product(
                            product_id=prior.product_id,
                            execute_tool=execute_tool,
                            variant_id=prior.variant_id,
                        )
                    return StoryResolutionResult(
                        resolved=bool(product) and not tray_failed,
                        story_media_id=media_id,
                        match_status="matched",
                        catalog_item_key=prior.catalog_item_key,
                        product_id=prior.product_id,
                        variant_id=prior.variant_id,
                        confidence=float(prior.match_confidence or 0.95),
                        product_payload=product,
                        question_type=question_type,
                        reply_hint=_compose_reply(
                            question_type=question_type,
                            product=product,
                            status="matched",
                            candidates=[],
                            tray_failed=tray_failed,
                        ),
                        shadow_only=shadow_only,
                        metrics=metrics,
                    )
        except StoryMediaError as exc:
            if exc.code in {"host_not_allowed", "scheme_not_https", "private_ip_blocked"}:
                repo.mark_failed(
                    tenant_id=tenant,
                    provider=provider,
                    instagram_account_id=account,
                    story_media_id=media_id,
                    explanation={"reason": exc.code},
                )
            else:
                repo.mark_expired(
                    tenant_id=tenant,
                    provider=provider,
                    instagram_account_id=account,
                    story_media_id=media_id,
                    explanation={"reason": exc.code},
                )
            return StoryResolutionResult(
                resolved=False,
                story_media_id=media_id,
                match_status="expired" if "http" in exc.code or exc.code == "download_failed" else "failed",
                failure_reason=exc.code,
                needs_clarification=True,
                question_type=question_type,
                reply_hint=_compose_reply(
                    question_type=question_type,
                    product=None,
                    status="failed",
                    candidates=[],
                    expired=True,
                ),
                shadow_only=shadow_only,
                metrics=metrics,
            )
    else:
        repo.mark_expired(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            explanation={"reason": "media_url_missing"},
        )
        return StoryResolutionResult(
            resolved=False,
            story_media_id=media_id,
            match_status="expired",
            failure_reason="media_url_missing",
            needs_clarification=True,
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="failed",
                candidates=[],
                expired=True,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    if analysis is None:
        repo.mark_failed(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            explanation={"reason": "visual_analysis_missing"},
        )
        return StoryResolutionResult(
            resolved=False,
            story_media_id=media_id,
            match_status="failed",
            failure_reason="visual_analysis_missing",
            needs_clarification=True,
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="failed",
                candidates=[],
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    repo.save_visual_analysis(
        tenant_id=tenant,
        provider=provider,
        instagram_account_id=account,
        story_media_id=media_id,
        visual_analysis=analysis.model_dump(mode="json"),
        media_sha256=media_sha,
        media_storage_path=storage_path,
    )

    if analysis.multiple_products or analysis.watch_count > 1:
        repo.mark_ambiguous(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            explanation={"reason": "multiple_products_visible"},
            confidence=float(analysis.product_identity_confidence or 0.5),
        )
        metrics["story_ambiguous_matches"] = 1
        log_event("instagram_story.ambiguous", {"reason": "multiple_products"})
        return StoryResolutionResult(
            resolved=False,
            story_media_id=media_id,
            match_status="ambiguous",
            needs_clarification=True,
            clarification_options=["mostrador azul", "mostrador preto"],
            confidence=float(analysis.product_identity_confidence or 0.5),
            question_type=question_type,
            reply_hint=(
                "Nesse Story aparecem mais de um relógio. "
                "Você quer saber o valor de qual deles?"
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    from .tray_tools import execute_tool as default_execute

    tool = execute_tool or default_execute
    candidates = await match_story_to_catalog(
        tenant_id=tenant,
        analysis=analysis,
        execute_tool=tool,
    )
    repo.save_candidates(
        tenant_id=tenant,
        provider=provider,
        instagram_account_id=account,
        story_media_id=media_id,
        candidates=[c.model_dump(mode="json") for c in candidates],
        explanation={"analysis_version": getattr(get_settings(), "instagram_story_analysis_version", "v1")},
    )
    status, top = classify_match(
        candidates,
        multiple_products=bool(analysis.multiple_products),
    )

    if status == "matched" and top is not None:
        repo.confirm_match(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            catalog_item_key=top.catalog_item_key,
            product_id=top.product_id,
            variant_id=top.variant_id,
            match_source=(
                "visual_exact_reference"
                if any(r.startswith(("ean:", "sku:", "reference:")) for r in top.match_reasons)
                else "visual_similarity"
            ),
            match_confidence=top.score,
            explanation={"reasons": top.match_reasons},
        )
        log_event("instagram_story.matched", {"confidence": top.score})
        product = None
        tray_failed = False
        if tool is not None:
            log_event("instagram_story.revalidation_started", {"product_id": top.product_id})
            product, tray_failed = await _revalidate_product(
                product_id=top.product_id,
                execute_tool=tool,
                variant_id=top.variant_id,
            )
            if tray_failed:
                log_event("instagram_story.revalidation_failed", {"product_id": top.product_id})
        evidence = []
        if product:
            authorized, grounded = authorize_products_for_responder(
                [product],
                tenant_id=tenant,
            )
            product = authorized[0] if authorized else None
            evidence = [g.model_dump(mode="json") for g in grounded]
        return StoryResolutionResult(
            resolved=bool(product) and not tray_failed,
            story_media_id=media_id,
            match_status="matched",
            catalog_item_key=top.catalog_item_key,
            product_id=top.product_id,
            variant_id=top.variant_id,
            confidence=top.score,
            candidates=candidates[:5],
            factual_evidence=evidence,
            product_payload=product,
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=product,
                status="matched",
                candidates=candidates,
                tray_failed=tray_failed,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    if status == "ambiguous":
        repo.mark_ambiguous(
            tenant_id=tenant,
            provider=provider,
            instagram_account_id=account,
            story_media_id=media_id,
            explanation={"top": candidates[0].model_dump(mode="json") if candidates else {}},
            confidence=candidates[0].score if candidates else 0.0,
        )
        metrics["story_ambiguous_matches"] = 1
        log_event("instagram_story.ambiguous", {"confidence": candidates[0].score if candidates else 0})
        return StoryResolutionResult(
            resolved=False,
            story_media_id=media_id,
            match_status="ambiguous",
            needs_clarification=True,
            candidates=candidates[:5],
            confidence=candidates[0].score if candidates else 0.0,
            question_type=question_type,
            reply_hint=_compose_reply(
                question_type=question_type,
                product=None,
                status="ambiguous",
                candidates=candidates,
            ),
            shadow_only=shadow_only,
            metrics=metrics,
        )

    repo.mark_not_found(
        tenant_id=tenant,
        provider=provider,
        instagram_account_id=account,
        story_media_id=media_id,
        explanation={"candidate_count": len(candidates)},
    )
    log_event("instagram_story.not_found", {"candidate_count": len(candidates)})
    return StoryResolutionResult(
        resolved=False,
        story_media_id=media_id,
        match_status="not_found",
        needs_clarification=True,
        candidates=candidates[:5],
        question_type=question_type,
        reply_hint=_compose_reply(
            question_type=question_type,
            product=None,
            status="not_found",
            candidates=candidates,
        ),
        shadow_only=shadow_only,
        metrics=metrics,
    )


def story_result_to_agent_result(
    resolution: StoryResolutionResult,
    *,
    incoming: IncomingMessage,
) -> AgentResult | None:
    """Convert resolution into an AgentResult. Shadow mode returns None (no reply change)."""
    if resolution.shadow_only:
        log_event(
            "instagram_story.reply_generated",
            {"shadow": True, "status": resolution.match_status},
        )
        return None
    reply = (resolution.reply_hint or "").strip()
    if not reply:
        return None
    metadata: dict[str, Any] = {
        "domain": "commerce",
        "instagram_story": True,
        "story_match_status": resolution.match_status,
        "story_confidence": resolution.confidence,
        "story_metrics": resolution.metrics,
        "tenant_id": _tenant_id(),
    }
    commercial: dict[str, Any] = {}
    if resolution.product_payload:
        commercial["products"] = [resolution.product_payload]
        metadata["presented_products"] = True
        metadata["active_product"] = {
            "product_id": resolution.product_id,
            "variant_id": resolution.variant_id,
            "name": resolution.product_payload.get("name"),
            "url": resolution.product_payload.get("url"),
        }
        metadata["last_story_product"] = StoryConversationReference(
            story_media_id=resolution.story_media_id,
            catalog_item_key=resolution.catalog_item_key,
            product_id=resolution.product_id,
            variant_id=resolution.variant_id,
            match_status=resolution.match_status,
            confidence=resolution.confidence,
            resolved_at=datetime.now(timezone.utc),
        ).model_dump(mode="json")
        if resolution.factual_evidence:
            metadata["grounded_commerce_evidence"] = resolution.factual_evidence
        allowed = {
            "allowed_product_ids": [resolution.product_id] if resolution.product_id else [],
            "allowed_variant_ids": [resolution.variant_id] if resolution.variant_id else [],
            "allowed_catalog_item_keys": (
                [resolution.catalog_item_key] if resolution.catalog_item_key else []
            ),
        }
        metadata["allowed_id_sets"] = allowed
    log_event(
        "instagram_story.reply_generated",
        {
            "status": resolution.match_status,
            "resolved": resolution.resolved,
            "question_type": resolution.question_type.value,
        },
    )
    _ = incoming
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason=(
            None
            if resolution.resolved
            else resolution.failure_reason or resolution.match_status
        ),
        commercial_data=commercial or None,
        response_metadata=metadata,
    )
