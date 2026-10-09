"""Job Command private live voice session API.

Designed for a Vercel/Next.js application to call server-to-server.  The browser
gets a one-use, short-lived provider credential, never an ElevenLabs API key.
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..auth import require_job_command_api_key, require_job_command_enabled
from ..config import settings
from ..services import elevenlabs_agents, voice_mint_limits, voice_router, voice_sessions


# PR21-F2: every Job Command route is behind JOB_COMMAND_VOICE_ENABLED (default
# OFF -> 404). Session routes authenticate with JOB_COMMAND_API_KEY only; the
# MSE key is refused. ApiKeyMiddleware enforces the same rules (defense in depth).
router = APIRouter(tags=["job-command-voice"], dependencies=[Depends(require_job_command_enabled)])


class VoiceSessionCreate(BaseModel):
    """An authenticated application request for a short-lived live voice session."""

    visitor_ref: str = Field(min_length=1, max_length=128, description="Opaque authenticated application-user reference")
    language: str | None = Field(default=None, max_length=16, description="Requested supported BCP-47 language")
    surface: Literal["web", "phone"] = "web"
    archive_consent: bool = Field(
        default=False,
        description="Opt in to saving post-call audio for Gemini diarized transcription and QA.",
    )

    @field_validator("visitor_ref")
    @classmethod
    def normalize_visitor_ref(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("visitor_ref cannot be blank")
        return value


class ConversationBind(BaseModel):
    provider_conversation_id: str = Field(min_length=1, max_length=255)


def _client_ip(request: Request) -> str:
    if settings.job_command_trust_forwarded_for:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else ""


def _voice_error(exc: Exception) -> HTTPException:
    if isinstance(exc, voice_mint_limits.VoiceMintLimited):
        headers = {"Retry-After": str(exc.retry_after_seconds)} if exc.retry_after_seconds else None
        return HTTPException(429, str(exc), headers=headers)
    if isinstance(exc, voice_sessions.VoiceSessionConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, (voice_router.VoiceRouteError, voice_sessions.VoiceSessionError)):
        return HTTPException(422, str(exc))
    if isinstance(exc, elevenlabs_agents.ElevenLabsAgentNotConfigured):
        return HTTPException(503, str(exc))
    if isinstance(exc, elevenlabs_agents.ElevenLabsAgentError):
        return HTTPException(502, str(exc))
    return HTTPException(500, "Job Command live voice request failed unexpectedly.")


@router.post("/voice/sessions", status_code=201)
async def issue_session(
    body: VoiceSessionCreate,
    request: Request,
    presented_key: str = Depends(require_job_command_api_key),
):
    """Return a short-lived provider signed URL and safe conversation initialization data.

    A Vercel route must authenticate its own signed-in user before forwarding an
    equivalent request; the opaque ``visitor_ref`` must be server-derived, not
    trusted directly from a browser form.
    """
    try:
        return await voice_sessions.create_session(
            visitor_ref=body.visitor_ref,
            requested_language=body.language,
            surface=body.surface,
            archive_consent=body.archive_consent,
            api_key_fingerprint=voice_mint_limits.fingerprint(presented_key),
            client_ip_fingerprint=voice_mint_limits.fingerprint(_client_ip(request)),
        )
    except Exception as exc:
        raise _voice_error(exc) from exc


@router.get("/voice/sessions/{session_id}", dependencies=[Depends(require_job_command_api_key)])
async def session_detail(session_id: UUID):
    session = await voice_sessions.get_session(session_id)
    if not session:
        raise HTTPException(404, "voice session not found")
    # The persistence layer never contains a provider signed URL. This response
    # is therefore safe for an authenticated operations dashboard.
    return session


@router.post("/voice/sessions/{session_id}/provider-conversation", dependencies=[Depends(require_job_command_api_key)])
async def bind_provider_conversation(session_id: UUID, body: ConversationBind):
    """Bind the provider conversation ID after the browser session starts."""
    try:
        return await voice_sessions.bind_conversation(
            session_id, provider_conversation_id=body.provider_conversation_id
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise _voice_error(exc) from exc


@router.post("/webhooks/elevenlabs/voice")
async def elevenlabs_voice_webhook(request: Request):
    """Verify and durably accept a post-call ElevenLabs event.

    The raw bytes are verified before parsing. Raw audio never enters Postgres
    (PR21-F1); audio bytes are written to the archive store only for a session
    whose ``archive_consent`` is true, then a separate worker performs Gemini
    file transcription so the webhook returns promptly.
    """
    raw_body = await request.body()
    try:
        event = elevenlabs_agents.verify_postcall_webhook(
            raw_body,
            request.headers.get("elevenlabs-signature"),
        )
    except elevenlabs_agents.ElevenLabsAgentError as exc:
        raise HTTPException(401, str(exc)) from exc
    try:
        return await voice_sessions.ingest_postcall_event(event)
    except Exception as exc:
        raise _voice_error(exc) from exc
