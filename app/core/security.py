import hmac
from fastapi import Header, HTTPException, Request
from .config import get_settings


def _secure_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def verify_brevo_webhook(request: Request, x_webhook_token: str | None = Header(default=None)) -> None:
    """Validate the Brevo secret, preferring ``X-Webhook-Token``.

    Brevo Conversations calls the configured webhook URL without support for a
    custom authentication header, so existing integrations carry the secret in
    ``?token=``.  Request observability must keep query values redacted.
    """
    settings = get_settings()
    if not settings.brevo_webhook_secret:
        print("[brevo.webhook.auth] webhook_secret_not_configured")
        if settings.environment.lower() == "production":
            raise HTTPException(status_code=500, detail="webhook_secret_not_configured")
        return

    header_token = str(x_webhook_token or "").strip()
    query_tokens = [
        str(value or "").strip()
        for value in request.query_params.getlist("token")
        if str(value or "").strip()
    ]
    # A supplied header is authoritative.  Do not let a valid query parameter
    # rescue an invalid header, which would make authentication ambiguous.
    provided_token = header_token or (query_tokens[0] if len(query_tokens) == 1 else "")

    if not provided_token:
        print("[brevo.webhook.auth] missing_webhook_token")
        raise HTTPException(status_code=401, detail="invalid_webhook_token")

    if provided_token == "replace-with-a-random-secret":
        print("[brevo.webhook.auth] placeholder_token_in_request")
        raise HTTPException(status_code=401, detail="invalid_webhook_token")

    if not _secure_equals(provided_token, settings.brevo_webhook_secret):
        print("[brevo.webhook.auth] invalid_webhook_token")
        raise HTTPException(status_code=401, detail="invalid_webhook_token")

    if not header_token:
        print("[brevo.webhook.auth] authenticated_via_query_token")


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
