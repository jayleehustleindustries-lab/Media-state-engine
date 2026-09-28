"""Isolation and validation controls shared by the Job Command campaign MCP/service."""
from __future__ import annotations

import base64
from hashlib import sha256
import hmac
import json
import os
import re
import time
from typing import Any
from urllib.parse import unquote
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError


FORBIDDEN_KEYS = {
    "jayleefit_job_id", "jayleefit_user_id", "jayleefit_asset_uri",
    "media_state_job_id", "shared_pipeline_id", "cross_project_token",
    # PR21-F5: common aliases for MSE / HeyGen render identifiers.
    "media_state_engine", "media_state_engine_job_id", "mse_job_id",
    "heygen_video_id", "heygen_job_id", "heygen_avatar_id",
}
# PR21-F5: substrings matched against a normalized form (lowercase, only
# [a-z0-9], percent-decoded) of metadata keys/values, page_url, route and
# visitor_ref. Normalization defeats "media_state" / "Media-State" / "%6Aayleefit".
FORBIDDEN_VALUE_TOKENS = ("jayleefit", "mediastate")
# Keys additionally may not reference MSE HeyGen renders or an ``mse_`` prefix.
FORBIDDEN_KEY_TOKENS = FORBIDDEN_VALUE_TOKENS + ("heygen", "crossproject", "sharedpipeline")

# Events that capture or render a page: they must target an allowlisted public
# host + exact route (default deny; see CapturePolicy).
CAPTURE_CLASS_EVENTS = {
    "campaign.capture_requested",
    "campaign.clip_brief_requested",
    "campaign.render_requested",
}
RENDER_EVENT = "campaign.render_requested"
RENDER_APPROVAL_SCOPE = "campaign.render"
PENDING_APPROVAL = "pending_approval"
MAX_APPROVAL_TTL_SECONDS = 24 * 60 * 60
ALLOWED_EVENTS = {
    "landing_page.cta_clicked",
    "campaign.capture_requested",
    "campaign.clip_brief_requested",
    "campaign.render_requested",
    "campaign.approval_requested",
}


class JobCommandEvent(BaseModel):
    """A non-sensitive event for the isolated Job Command campaign bus."""

    event_id: UUID
    event_type: str = Field(pattern=r"^([a-z_]+\.)+[a-z_]+$")
    campaign_id: UUID
    occurred_at: str
    source: str = Field(pattern=r"^(vercel|operator|job-command-mcp)$")
    visitor_ref: str | None = Field(default=None, max_length=128)
    page_url: HttpUrl | None = None
    route: str | None = Field(default=None, max_length=256)
    consent_to_capture: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    # PR21-F3: render events must carry a signed approval record.
    approval_ref: str | None = Field(default=None, max_length=128)
    approval_token: str | None = Field(default=None, max_length=4096)

    @field_validator("event_type")
    @classmethod
    def allowed_event_type(cls, value: str) -> str:
        if value not in ALLOWED_EVENTS:
            raise ValueError(f"Unsupported Job Command event type: {value}")
        return value

    @field_validator("route")
    @classmethod
    def public_route_only(cls, value: str | None) -> str | None:
        if value and (not value.startswith("/") or ".." in value or "//" in value):
            raise ValueError("route must be a clean public path beginning with '/'.")
        return value

    @model_validator(mode="after")
    def enforce_pipeline_separation(self):
        if _has_forbidden_reference(self):
            raise ValueError("Job Command events cannot include JayLeeFit or shared-pipeline data.")
        if self.event_type == "campaign.capture_requested" and not self.consent_to_capture:
            raise ValueError("Campaign capture requires explicit consent_to_capture=true.")
        if self.event_type in CAPTURE_CLASS_EVENTS:
            CapturePolicy.from_env().enforce(self)
        if self.event_type == RENDER_EVENT:
            # PR21-F3: fail closed. No verified approval record => never render.
            verify_render_approval(self)
        return self


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unquote(unquote(value)).lower())


def _iter_strings(value: Any, *, keys: bool):
    if isinstance(value, dict):
        for key, item in value.items():
            if keys:
                yield str(key)
            yield from _iter_strings(item, keys=keys)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item, keys=keys)
    elif not keys and value is not None and not isinstance(value, bool):
        yield str(value)


def _has_forbidden_reference(event: "JobCommandEvent") -> bool:
    for key in _iter_strings(event.metadata, keys=True):
        lowered = key.lower()
        norm = _normalize(key)
        if lowered in FORBIDDEN_KEYS or lowered.startswith("mse_") or any(t in norm for t in FORBIDDEN_KEY_TOKENS):
            return True
    scanned = list(_iter_strings(event.metadata, keys=False))
    scanned += [str(v) for v in (event.page_url, event.route, event.visitor_ref) if v]
    return any(token in _normalize(text) for text in scanned for token in FORBIDDEN_VALUE_TOKENS)


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


class CapturePolicy(BaseModel):
    """PR21-F5: config-driven capture allowlist. Empty config denies everything.

    JOB_COMMAND_CAPTURE_ALLOWED_HOSTS   comma list of exact hostnames
    JOB_COMMAND_CAPTURE_ALLOWED_ROUTES  comma list of exact public paths
    """

    allowed_hosts: frozenset[str] = frozenset()
    allowed_routes: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> "CapturePolicy":
        return cls(
            allowed_hosts=frozenset(h.lower().rstrip(".") for h in _csv_env("JOB_COMMAND_CAPTURE_ALLOWED_HOSTS")),
            allowed_routes=frozenset(_csv_env("JOB_COMMAND_CAPTURE_ALLOWED_ROUTES")),
        )

    def enforce(self, event: "JobCommandEvent") -> None:
        if not event.page_url or not event.route:
            raise ValueError("Capture/render events require an allowlisted page_url and route.")
        url = event.page_url
        host = (url.host or "").lower().rstrip(".")
        if url.scheme != "https":
            raise ValueError("Capture page_url must use https.")
        if url.username or url.password:
            raise ValueError("Capture page_url must not contain credentials.")
        if url.port not in (None, 443):
            raise ValueError("Capture page_url must use the default https port.")
        if url.query or url.fragment:
            raise ValueError("Capture page_url must not contain a query string or fragment.")
        if not self.allowed_hosts or host not in self.allowed_hosts:
            raise ValueError("Capture page_url host is not on the Job Command capture allowlist.")
        if not self.allowed_routes or event.route not in self.allowed_routes:
            raise ValueError("Capture route is not on the Job Command capture allowlist.")
        if (url.path or "/") != event.route:
            raise ValueError("Capture page_url path must exactly match the allowlisted route.")


# --- PR21-F3: signed approval records for render requests -------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _approval_secret() -> str:
    """Approval secret held only by the operator approval tool and verifiers.

    It must differ from JOB_COMMAND_INGRESS_SECRET so an ingress client (e.g.
    Vercel) that can sign events cannot also forge approvals.
    """
    secret = os.getenv("JOB_COMMAND_APPROVAL_SECRET", "").strip()
    ingress = os.getenv("JOB_COMMAND_INGRESS_SECRET", "").strip()
    if not secret:
        raise _pending("JOB_COMMAND_APPROVAL_SECRET is not configured; renders stay pending approval.")
    if ingress and hmac.compare_digest(secret.encode("utf-8"), ingress.encode("utf-8")):
        raise _pending("JOB_COMMAND_APPROVAL_SECRET must differ from JOB_COMMAND_INGRESS_SECRET.")
    return secret


def _pending(message: str) -> PydanticCustomError:
    return PydanticCustomError(PENDING_APPROVAL, "Render is pending human approval: {reason}", {"reason": message})


def mint_approval_token(
    *,
    approval_id: str,
    campaign_id: str | UUID,
    approved_by: str,
    secret: str,
    ttl_seconds: int = 3600,
    now: int | None = None,
) -> str:
    """Operator-side helper to sign a human approval record for one campaign.

    Never expose this through an MCP tool or the ingress: it is for the
    separate human approval workflow that holds JOB_COMMAND_APPROVAL_SECRET.
    """
    if not secret:
        raise ValueError("approval secret is required")
    if not approved_by.strip():
        raise ValueError("approved_by is required")
    issued = int(time.time() if now is None else now)
    claims = {
        "v": 1,
        "approval_id": approval_id,
        "campaign_id": str(campaign_id),
        "scope": RENDER_APPROVAL_SCOPE,
        "status": "approved",
        "approved_by": approved_by.strip(),
        "approved_at": issued,
        "expires_at": issued + int(ttl_seconds),
    }
    body = _b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), f"v1.{body}".encode("ascii"), sha256).hexdigest()
    return f"v1.{body}.{sig}"


def verify_render_approval(event: "JobCommandEvent", *, now: int | None = None) -> dict[str, Any]:
    """Return verified approval claims or raise a ``pending_approval`` error."""
    if not event.approval_ref or not event.approval_token:
        raise _pending("campaign.render_requested requires approval_ref and a signed approval_token.")
    secret = _approval_secret()
    try:
        version, body, sig = event.approval_token.split(".")
    except ValueError:
        raise _pending("approval_token is malformed.") from None
    expected = hmac.new(secret.encode("utf-8"), f"{version}.{body}".encode("ascii"), sha256).hexdigest()
    if version != "v1" or not hmac.compare_digest(sig.lower(), expected):
        raise _pending("approval_token signature is invalid.")
    try:
        claims = json.loads(_b64url_decode(body))
    except (ValueError, json.JSONDecodeError):
        raise _pending("approval_token claims are unreadable.") from None
    current = int(time.time() if now is None else now)
    if not isinstance(claims, dict) or claims.get("status") != "approved":
        raise _pending("approval record is not in the approved state.")
    if claims.get("scope") != RENDER_APPROVAL_SCOPE:
        raise _pending("approval record scope does not cover renders.")
    if claims.get("approval_id") != event.approval_ref:
        raise _pending("approval record does not match approval_ref.")
    if claims.get("campaign_id") != str(event.campaign_id):
        raise _pending("approval record belongs to a different campaign.")
    if not str(claims.get("approved_by") or "").strip():
        raise _pending("approval record has no approver.")
    try:
        approved_at = int(claims["approved_at"])
        expires_at = int(claims["expires_at"])
    except (KeyError, TypeError, ValueError):
        raise _pending("approval record has invalid timestamps.") from None
    if expires_at <= current:
        raise _pending("approval record has expired.")
    if expires_at - approved_at > MAX_APPROVAL_TTL_SECONDS or approved_at > current + 300:
        raise _pending("approval record validity window is not acceptable.")
    return claims


def is_pending_approval(exc: Exception) -> bool:
    """True when validation failed only because a render lacks a valid approval."""
    if isinstance(exc, PydanticCustomError):
        return exc.type == PENDING_APPROVAL
    if isinstance(exc, ValidationError):
        return any(err.get("type") == PENDING_APPROVAL for err in exc.errors())
    return False


def verify_hmac(raw_body: bytes, signature: str | None, secret: str) -> None:
    """Verify an HMAC SHA-256 signature from the approved ingress adapter."""
    if not secret:
        raise ValueError("JOB_COMMAND_INGRESS_SECRET is not configured.")
    if not signature:
        raise ValueError("Missing ingress signature.")
    presented = signature.removeprefix("sha256=").strip().lower()
    expected = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
    if not hmac.compare_digest(presented, expected):
        raise ValueError("Invalid ingress signature.")


def canonical_event_payload(event: JobCommandEvent) -> bytes:
    """Canonical JSON for Pub/Sub publication and idempotency hashing."""
    return json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")


def idempotency_key(event: JobCommandEvent) -> str:
    return sha256(canonical_event_payload(event)).hexdigest()
