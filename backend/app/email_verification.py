from urllib.parse import quote

import httpx

from app.config import settings


class EmailDeliveryError(Exception):
    pass


def send_verification_email(email: str, token: str) -> None:
    api_key = settings.resend_api_key
    if api_key is None or not settings.resend_from_email.strip():
        raise EmailDeliveryError("Email delivery is not configured")

    verification_url = (
        f"{settings.email_verification_url.rstrip('/#')}"
        f"/#verify-email/{quote(token, safe='')}"
    )
    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            json={
                "from": settings.resend_from_email,
                "to": [email],
                "subject": "Verify your Trading Copilot email",
                "text": (
                    "Verify your email address to unlock broker connections: "
                    f"{verification_url}\n\nThis link expires in 24 hours."
                ),
            },
            timeout=15,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        raise EmailDeliveryError("Email provider request failed") from None
