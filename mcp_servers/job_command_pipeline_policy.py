"""Isolation and validation controls shared by the Job Command campaign MCP/service."""
from __future__ import annotations

from hashlib import sha256
import hmac
import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


FORBIDDEN_KEYS = {
    "jayleefit_job_id", "jayleefit_user_id", "jayleefit_asset_uri",
    "media_state_job_id", "shared_pipeline_id", "cross_project_token",
}
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
        text = json.dumps(self.metadata, sort_keys=True).lower()
        if any(key in self.metadata for key in FORBIDDEN_KEYS) or "jayleefit" in text or "media-state-engine" in text:
            raise ValueError("Job Command events cannot include JayLeeFit or shared-pipeline data.")
        if self.event_type == "campaign.capture_requested" and not self.consent_to_capture:
            raise ValueError("Campaign capture requires explicit consent_to_capture=true.")
        return self


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
