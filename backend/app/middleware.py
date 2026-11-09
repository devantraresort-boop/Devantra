import logging
import time
import uuid

from starlette.responses import JSONResponse

from backend.app.config import settings

logger = logging.getLogger("devantara.requests")


class RequestProtectionMiddleware:
    """Bound request bodies (including chunked bodies) and add response security headers."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = 500

        async def protected_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers += [(b"x-request-id", request_id.encode()), (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
                csp = ("default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
                       "form-action 'self'; img-src 'self' https: data:; font-src 'self' https://fonts.gstatic.com; "
                       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                       "script-src 'self' https://*.razorpay.com; "
                       "connect-src 'self' https://*.razorpay.com; frame-src https://*.razorpay.com;")
                if scope["path"] not in {"/docs", "/redoc"}:
                    headers.append((b"content-security-policy", csp.encode()))
                if scope["path"].startswith("/api/") or scope["path"] == "/admin":
                    headers.append((b"cache-control", b"no-store"))
                if settings.environment == "production":
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = headers
            await send(message)

        headers = dict(scope.get("headers", []))
        try:
            declared_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared_length = -1
        if declared_length < 0:
            return await JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)(scope, receive, protected_send)
        if declared_length > settings.max_request_bytes:
            return await JSONResponse({"detail": "Request body is too large."}, status_code=413)(scope, receive, protected_send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > settings.max_request_bytes:
                return await JSONResponse({"detail": "Request body is too large."}, status_code=413)(scope, receive, protected_send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay_body():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        try:
            await self.app(scope, replay_body, protected_send)
        finally:
            route = scope.get("route")
            # Never log query strings, guest references, credentials or bodies.
            logger.info("request_id=%s method=%s route=%s status=%s duration_ms=%.1f",
                        request_id, scope["method"], getattr(route, "path", "unmatched"), status,
                        (time.monotonic() - started) * 1000)
