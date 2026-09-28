"""Narrow ElevenLabs Conversational AI adapter for private Job Command sessions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import settings


class ElevenLabsAgentError(RuntimeError):
    """Expected private-agent provider error safe to show at the API boundary."""


class ElevenLabsAgentNotConfigured(ElevenLabsAgentError):
    pass


def _configured() -> tuple[str, str, str]:
    api_key = (settings.elevenlabs_api_key or "").strip()
    agent_id = (settings.elevenlabs_agent_id or "").strip()
    base_url = (settings.elevenlabs_agents_api_url or "https://api.elevenlabs.io").rstrip("/")
    if not api_key or not agent_id:
        raise ElevenLabsAgentNotConfigured(
            "Job Command live voice is not configured: set ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID."
        )
    return api_key, agent_id, base_url


async def create_signed_url() -> dict[str, Any]:
    """Get an ephemeral browser credential for one private ElevenLabs conversation.

    The returned URL is a bearer credential.  Callers must return it only to an
    authenticated application user and must never persist it, log it, or put it
    into analytics.  ElevenLabs documents a 15-minute start window.
    """
    api_key, agent_id, base_url = _configured()
    headers = {"xi-api-key": api_key, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{base_url}/v1/convai/conversation/get-signed-url",
            headers=headers,
            params={"agent_id": agent_id},
        )
    if response.status_code in (401, 403):
        raise ElevenLabsAgentError("ElevenLabs rejected the live-agent credential.")
    if not response.is_success:
        raise ElevenLabsAgentError(f"ElevenLabs signed-URL request failed with HTTP {response.status_code}.")
    try:
        body = response.json()
    except ValueError as exc:
        raise ElevenLabsAgentError("ElevenLabs signed-URL response was not JSON.") from exc
    signed_url = body.get("signed_url") if isinstance(body, dict) else None
    if not isinstance(signed_url, str) or not signed_url.startswith("wss://"):
        raise ElevenLabsAgentError("ElevenLabs signed-URL response did not include a secure WebSocket URL.")
    return {
        "signed_url": signed_url,
        "agent_id": agent_id,
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=settings.voice_session_token_ttl_seconds),
    }


def verify_postcall_webhook(raw_body: bytes, signature: str | None) -> dict[str, Any]:
    """Verify an ElevenLabs post-call webhook using the provider's official SDK.

    No hand-rolled HMAC is used: the SDK validates the provider signature and
    timestamp against the raw body.  This fails closed if the SDK or secret is
    absent rather than accepting a request with an incompatible signature shape.
    """
    secret = (settings.elevenlabs_agents_webhook_secret or settings.elevenlabs_webhook_secret or "").strip()
    if not secret:
        raise ElevenLabsAgentError("ElevenLabs Agents webhook secret is not configured.")
    if not signature:
        raise ElevenLabsAgentError("Missing ElevenLabs-Signature header.")
    try:
        from elevenlabs import ElevenLabs
        from elevenlabs.errors import BadRequestError
    except ImportError as exc:  # pragma: no cover - dependency contract
        raise ElevenLabsAgentError("ElevenLabs SDK is not installed for webhook verification.") from exc
    try:
        client = ElevenLabs(api_key=(settings.elevenlabs_api_key or "").strip())
        event = client.webhooks.construct_event(
            rawBody=raw_body.decode("utf-8"),
            sig_header=signature,
            secret=secret,
        )
    except (BadRequestError, UnicodeDecodeError, ValueError) as exc:
        raise ElevenLabsAgentError("Invalid ElevenLabs webhook signature or payload.") from exc
    if not isinstance(event, dict):
        raise ElevenLabsAgentError("ElevenLabs webhook parser returned an invalid event.")
    return event


__all__ = [
    "ElevenLabsAgentError",
    "ElevenLabsAgentNotConfigured",
    "create_signed_url",
    "verify_postcall_webhook",
]
