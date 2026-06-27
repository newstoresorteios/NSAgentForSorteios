import hmac
from fastapi import Header, HTTPException, Request
from .config import get_settings


def _secure_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def verify_brevo_webhook(request: Request, x_webhook_token: str | None = Header(default=None)) -> None:
    """Validate a simple shared-secret header for Brevo webhook calls.

    Configure Brevo to send header: X-Webhook-Token: <BREVO_WEBHOOK_SECRET>
    """
    settings = get_settings()
    if not settings.brevo_webhook_secret:
        # Never allow an unprotected webhook in production.
        if settings.environment.lower() == "production":
            raise HTTPException(status_code=500, detail="webhook_secret_not_configured")
        return

    query_token = request.query_params.get("token")
    provided_token = x_webhook_token or query_token

    if not provided_token or not _secure_equals(provided_token, settings.brevo_webhook_secret):
        raise HTTPException(status_code=401, detail="invalid_webhook_token")


async def verify_admin_token(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.admin_api_token:
        raise HTTPException(status_code=500, detail="admin_token_not_configured")

    expected = f"Bearer {settings.admin_api_token}"
    if not authorization or not _secure_equals(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid_admin_token")
