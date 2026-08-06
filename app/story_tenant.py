"""Resolve tenant for Instagram Story flows from authenticated channel context.

Never accept tenant_id from the customer message body.
Never silently fall back to \"newstore\".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .config import get_settings
from .observability import log_event


class TenantResolution(BaseModel):
    ok: bool = False
    tenant_id: str | None = None
    source: Literal[
        "explicit",
        "integration",
        "instagram_account_map",
        "persona_settings",
        "unresolved",
    ] = "unresolved"
    failure_code: str | None = None
    instagram_account_id: str | None = None


# Optional static map: INSTAGRAM_STORY_ACCOUNT_TENANT_MAP=ig_biz_1:tenant_a,ig_biz_2:tenant_b
def _account_tenant_map() -> dict[str, str]:
    settings = get_settings()
    raw = str(getattr(settings, "instagram_story_account_tenant_map", "") or "")
    mapping: dict[str, str] = {}
    for part in raw.split(","):
        chunk = part.strip()
        if not chunk or ":" not in chunk:
            continue
        account, tenant = chunk.split(":", 1)
        account_id = account.strip()
        tenant_id = tenant.strip()
        if account_id and tenant_id:
            mapping[account_id] = tenant_id
    return mapping


async def resolve_story_tenant(
    *,
    provider: str,
    instagram_account_id: str,
    integration_id: str | None = None,
    explicit_tenant_id: str | None = None,
) -> TenantResolution:
    """Resolve tenant for Story catalog / association lookups."""
    _ = provider
    account = str(instagram_account_id or "").strip()
    explicit = str(explicit_tenant_id or "").strip()
    if explicit:
        resolution = TenantResolution(
            ok=True,
            tenant_id=explicit,
            source="explicit",
            instagram_account_id=account or None,
        )
        log_event(
            "story_tenant_resolution",
            {"ok": True, "source": "explicit", "has_account": bool(account)},
        )
        return resolution

    mapping = _account_tenant_map()
    if account and account in mapping:
        resolution = TenantResolution(
            ok=True,
            tenant_id=mapping[account],
            source="instagram_account_map",
            instagram_account_id=account,
        )
        log_event(
            "story_tenant_resolution",
            {"ok": True, "source": "instagram_account_map"},
        )
        return resolution

    if integration_id:
        # Reserved for future integration→tenant table; no silent default.
        log_event(
            "story_tenant_resolution",
            {"ok": False, "source": "integration", "code": "integration_map_missing"},
        )

    settings = get_settings()
    persona_tenant = str(getattr(settings, "agent_persona_tenant_id", None) or "").strip()
    if persona_tenant:
        resolution = TenantResolution(
            ok=True,
            tenant_id=persona_tenant,
            source="persona_settings",
            instagram_account_id=account or None,
        )
        log_event(
            "story_tenant_resolution",
            {"ok": True, "source": "persona_settings"},
        )
        return resolution

    log_event(
        "story_tenant_resolution",
        {
            "ok": False,
            "source": "unresolved",
            "code": "story_tenant_unresolved",
            "has_account": bool(account),
        },
    )
    return TenantResolution(
        ok=False,
        tenant_id=None,
        source="unresolved",
        failure_code="story_tenant_unresolved",
        instagram_account_id=account or None,
    )


def require_tenant_id(resolution: TenantResolution) -> str:
    if not resolution.ok or not resolution.tenant_id:
        raise ValueError(resolution.failure_code or "story_tenant_unresolved")
    return resolution.tenant_id
