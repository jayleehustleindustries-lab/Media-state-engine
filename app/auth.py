"""Bearer / API-key auth for mutation and paid job routes.

Inbound provider webhooks MUST NOT use this key — they verify HMAC/signatures
in their own handlers.
"""
from __future__ import annotations

import hmac

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

# PR21-F2: Job Command voice is a separate trust domain. Its routes are behind
# JOB_COMMAND_VOICE_ENABLED (default OFF) and authenticate ONLY with
# JOB_COMMAND_API_KEY. The MSE key is never accepted for them, and the Job
# Command key is never accepted for MSE routes.
JOB_COMMAND_PREFIX = "/voice"
JOB_COMMAND_WEBHOOK_PATHS = frozenset({"/webhooks/elevenlabs/voice"})


def _keys_match(presented: str | None, expected: str) -> bool:
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def is_job_command_path(path: str) -> bool:
    return path == JOB_COMMAND_PREFIX or path.startswith(JOB_COMMAND_PREFIX + "/")


def is_job_command_webhook(path: str) -> bool:
    return path.rstrip("/") in JOB_COMMAND_WEBHOOK_PATHS


def job_command_auth_error(presented: str | None) -> tuple[int, str] | None:
    """Return (status, detail) when a Job Command request must be refused.

    Fail closed: feature flag off -> 404 (routes are hidden); key unset or equal
    to the MSE key -> 503 misconfiguration; wrong/missing key -> 401.
    """
    if not settings.job_command_voice_enabled:
        return 404, "Not Found"
    expected = settings.effective_job_command_api_key
    if not expected:
        return 503, "Job Command auth is not configured: set JOB_COMMAND_API_KEY"
    mse_key = settings.effective_api_key
    if mse_key and hmac.compare_digest(expected.encode("utf-8"), mse_key.encode("utf-8")):
        return 503, "JOB_COMMAND_API_KEY must differ from the Media State Engine API key"
    if not _keys_match(presented, expected):
        return 401, "invalid or missing Job Command API key"
    return None


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
    if not _keys_match(presented, expected):
        raise HTTPException(401, "invalid or missing API key")
    return presented


def require_job_command_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_api_key: str | None = Security(_api_key_header),
) -> str:
    """FastAPI dependency for Job Command voice routes (separate key, flag-gated)."""
    presented = extract_presented_key(credentials, x_api_key)
    error = job_command_auth_error(presented)
    if error:
        raise HTTPException(*error)
    return presented or ""


def require_job_command_enabled() -> None:
    """Gate public Job Command provider webhooks behind the feature flag."""
    if not settings.job_command_voice_enabled:
        raise HTTPException(404, "Not Found")


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
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        if is_job_command_webhook(path):
            # Signature-verified in the handler, but hidden entirely when OFF.
            if not settings.job_command_voice_enabled:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return await call_next(request)
        if is_job_command_path(path):
            error = job_command_auth_error(_presented_from_headers(request))
            if error:
                return JSONResponse({"detail": error[1]}, status_code=error[0])
            return await call_next(request)
        if is_public_path(path):
            return await call_next(request)
        expected = _configured_key()
        if not expected:
            # Fail closed for mutations when key missing? Spec: require auth on
            # paid/mutation routes. Middleware fails closed if key unset so
            # unauthenticated open APIs cannot burn paid providers in prod.
            return JSONResponse({"detail": "API auth is not configured: set API_KEY or MEDIA_ENGINE_API_KEY"}, status_code=503)
        presented = _presented_from_headers(request)
        if not _keys_match(presented, expected):
            return JSONResponse({"detail": "invalid or missing API key"}, status_code=401)
        return await call_next(request)


def _presented_from_headers(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    presented = None
    if auth.lower().startswith("bearer "):
        presented = auth[7:].strip()
    if not presented:
        presented = (request.headers.get("x-api-key") or "").strip() or None
    return presented
