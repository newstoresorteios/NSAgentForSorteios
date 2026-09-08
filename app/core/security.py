import hmac
from fastapi import Header, HTTPException, Request
from .config import get_settings


def _secure_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def verify_brevo_webhook(request: Request, x_webhook_token: str | None = Header(default=None)) -> None:
    """Validate the shared secret on header X-Webhook-Token only.

    Query-string ?token= is ignored so the secret is not written to access logs.
    """
    settings = get_settings()
    if not settings.brevo_webhook_secret:
        print("[brevo.webhook.auth] webhook_secret_not_configured")
        if settings.environment.lower() == "production":
            raise HTTPException(status_code=500, detail="webhook_secret_not_configured")
        return

    provided_token = x_webhook_token

    if not provided_token:
        print("[brevo.webhook.auth] missing_webhook_token")
        raise HTTPException(status_code=401, detail="invalid_webhook_token")

    if provided_token == "replace-with-a-random-secret":
        print("[brevo.webhook.auth] placeholder_token_in_request")
        raise HTTPException(status_code=401, detail="invalid_webhook_token")

    if not _secure_equals(provided_token, settings.brevo_webhook_secret):
        print("[brevo.webhook.auth] invalid_webhook_token")
        raise HTTPException(status_code=401, detail="invalid_webhook_token")


async def verify_admin_token(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.admin_api_token:
        raise HTTPException(status_code=500, detail="admin_token_not_configured")

    expected = f"Bearer {settings.admin_api_token}"
    if not authorization or not _secure_equals(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid_admin_token")


async def verify_remarketing_cron(
    authorization: str | None = Header(default=None),
) -> None:
    settings = get_settings()
    if not settings.remarketing_cron_secret:
        raise HTTPException(status_code=500, detail="remarketing_cron_secret_not_configured")

    expected = f"Bearer {settings.remarketing_cron_secret}"
    if not authorization or not _secure_equals(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid_remarketing_cron_token")
