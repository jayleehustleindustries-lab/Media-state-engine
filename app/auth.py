"""Bearer / API-key auth for mutation and paid job routes.

Inbound provider webhooks MUST NOT use this key — they verify HMAC/signatures
in their own handlers.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import settings

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Paths that stay public (health + inbound provider webhooks).
PUBLIC_PREFIXES = ("/health", "/docs", "/openapi.json", "/redoc")
PUBLIC_EXACT = {"/", "/favicon.ico"}
WEBHOOK_PREFIX = "/webhooks/"


def _configured_key() -> str:
    return settings.effective_api_key


def extract_presented_key(
    authorization: HTTPAuthorizationCredentials | None,
    x_api_key: str | None,
) -> str | None:
    if authorization and authorization.scheme.lower() == "bearer" and authorization.credentials:
        return authorization.credentials.strip()
    if x_api_key:
        return x_api_key.strip()
    return None


def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_api_key: str | None = Security(_api_key_header),
) -> str:
    """FastAPI dependency: require a valid API key on protected routes."""
    expected = _configured_key()
    if not expected:
        raise HTTPException(503, "API auth is not configured: set API_KEY or MEDIA_ENGINE_API_KEY")
    presented = extract_presented_key(credentials, x_api_key)
    if not presented or presented != expected:
        raise HTTPException(401, "invalid or missing API key")
    return presented


def is_public_path(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    if any(path == p or path.startswith(p + "/") for p in PUBLIC_PREFIXES if p != "/health"):
        return True
    if path == "/health" or path.startswith("/health/"):
        return True
    if path.startswith(WEBHOOK_PREFIX):
        return True
    return False


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated requests to non-public routes when a key is configured.

    When no API key env is set, middleware allows all traffic but logs nothing —
    dependency-level require_api_key still returns 503 on explicit protected deps.
    Prefer setting MEDIA_ENGINE_API_KEY (or API_KEY) in every non-dev environment.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" or is_public_path(request.url.path):
            return await call_next(request)
        expected = _configured_key()
        if not expected:
            # Fail closed for mutations when key missing? Spec: require auth on
            # paid/mutation routes. Middleware fails closed if key unset so
            # unauthenticated open APIs cannot burn paid providers in prod.
            return JSONResponse({"detail": "API auth is not configured: set API_KEY or MEDIA_ENGINE_API_KEY"}, status_code=503)
        auth = request.headers.get("authorization") or ""
        presented = None
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
        if not presented:
            presented = (request.headers.get("x-api-key") or "").strip() or None
        if not presented or presented != expected:
            return JSONResponse({"detail": "invalid or missing API key"}, status_code=401)
        return await call_next(request)
