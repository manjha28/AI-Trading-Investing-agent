"""Pure-ASGI request timeout: a request still running after ``request_timeout_seconds`` is answered with 504 so the client is not held for minutes. The worker thread of a sync endpoint cannot be cancelled and
runs to its end, but the connection and the user's page are freed; the cause of slow endpoints is fixed at the source (stored daily report, bigger DB pool)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

EXEMPT_PREFIXES = ("/api/v1/market-stream", "/api/v1/ws", "/docs", "/openapi")


class RequestTimeoutMiddleware:
    def __init__(self, app: Any, seconds: float) -> None:
        self.app, self.seconds = app, seconds

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or self.seconds <= 0 or scope["path"].startswith(EXEMPT_PREFIXES) or scope["path"].endswith("/stream"):
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)
        try:
            await asyncio.wait_for(self.app(scope, receive, tracking_send), timeout=self.seconds)
        except asyncio.TimeoutError:
            if started:
                return
            body = json.dumps({"detail": {"code": "request_timeout", "message": f"The server took longer than {self.seconds:g} seconds to answer. Try again in a moment."}}).encode()
            await send({"type": "http.response.start", "status": 504, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
