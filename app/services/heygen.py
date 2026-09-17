"""HeyGen avatar-video API integration.

The service deliberately keeps provider I/O separate from database state changes.
Callers reserve their database idempotency key before calling ``create_video`` and
persist the returned provider video ID in the same job's asset metadata.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

import httpx

from ..config import settings


class HeyGenError(RuntimeError):
    """Base class for expected HeyGen integration errors."""


class HeyGenNotConfigured(HeyGenError):
    pass


class HeyGenAuthenticationError(HeyGenError):
    pass


class HeyGenRequestError(HeyGenError):
    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class InvalidHeyGenSignature(HeyGenError):
    pass


@dataclass(frozen=True)
class HeyGenVideo:
    video_id: str
    status: str = "processing"
    video_url: str | None = None
    thumbnail_url: str | None = None
    raw: Mapping[str, Any] | None = None


def _require_config() -> tuple[str, str]:
    api_key = getattr(settings, "heygen_api_key", "")
    base_url = getattr(settings, "heygen_api_url", "https://api.heygen.com").rstrip("/")
    if not api_key:
        raise HeyGenNotConfigured("HeyGen is not configured: set HEYGEN_API_KEY")
    return api_key, base_url


def _headers(api_key: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"X-Api-Key": api_key, "Accept": "application/json", "Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _raise_for_provider(response: httpx.Response) -> None:
    if response.is_success:
        return
    body = _json_or_text(response)
    if response.status_code in (401, 403):
        raise HeyGenAuthenticationError("HeyGen rejected the API key")
    message = body.get("error", {}).get("message") if isinstance(body, dict) else None
    raise HeyGenRequestError(message or f"HeyGen request failed with HTTP {response.status_code}", status_code=response.status_code, body=body)


def _data(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HeyGenRequestError("HeyGen returned a non-object response", body=body)
    data = body.get("data", body)
    if not isinstance(data, dict):
        raise HeyGenRequestError("HeyGen response has no object data field", body=body)
    return data


async def create_video(
    *,
    job_id: UUID | str,
    script_text: str,
    avatar_id: str | None = None,
    voice_id: str | None = None,
    audio_url: str | None = None,
    dimension: tuple[int, int] = (1080, 1920),
    background_color: str | None = None,
    callback_id: str | None = None,
    idempotency_key: str | None = None,
    engine: dict[str, Any] | None = None,
) -> HeyGenVideo:
    """Create an asynchronous HeyGen avatar video.

    Use exactly one audio mode: ``script_text`` with an optional ``voice_id`` or
    a public ``audio_url`` for lip-sync. The provider's idempotency header is
    populated when ``idempotency_key`` is supplied.
    """
    api_key, base_url = _require_config()
    configured_avatar = getattr(settings, "heygen_avatar_id", "")
    configured_voice = getattr(settings, "heygen_voice_id", "")
    avatar_id = avatar_id or configured_avatar
    voice_id = voice_id or configured_voice
    if not avatar_id:
        raise HeyGenRequestError("avatar_id is required")
    if not script_text and not audio_url:
        raise HeyGenRequestError("script_text or audio_url is required")
    if script_text and audio_url:
        raise HeyGenRequestError("script_text and audio_url are mutually exclusive")

    character: dict[str, Any] = {"type": "avatar", "avatar_id": avatar_id}
    voice: dict[str, Any]
    if audio_url:
        voice = {"type": "audio", "audio_url": audio_url}
    else:
        voice = {"type": "text", "input_text": script_text}
        if voice_id:
            voice["voice_id"] = voice_id

    scene: dict[str, Any] = {"character": character, "voice": voice}
    if background_color:
        scene["background"] = {"type": "color", "value": background_color}
    payload: dict[str, Any] = {
        "video_inputs": [scene],
        "dimension": {"width": dimension[0], "height": dimension[1]},
    }
    if callback_id:
        payload["callback_id"] = callback_id
    if engine:
        payload["engine"] = engine

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{base_url}/v3/videos", headers=_headers(api_key, idempotency_key), json=payload)
    _raise_for_provider(response)
    data = _data(response.json())
    video_id = data.get("video_id") or data.get("id")
    if not video_id:
        raise HeyGenRequestError("HeyGen response did not include video_id", body=data)
    return HeyGenVideo(video_id=str(video_id), status=str(data.get("status", "processing")), video_url=data.get("video_url"), thumbnail_url=data.get("thumbnail_url"), raw=data)


async def get_video(video_id: str) -> HeyGenVideo:
    """Fetch the current status of a HeyGen video, useful as a reconciliation fallback."""
    api_key, base_url = _require_config()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{base_url}/v3/videos/{video_id}", headers=_headers(api_key))
    _raise_for_provider(response)
    data = _data(response.json())
    return HeyGenVideo(video_id=str(data.get("video_id", video_id)), status=str(data.get("status", "unknown")), video_url=data.get("video_url"), thumbnail_url=data.get("thumbnail_url"), raw=data)


def verify_webhook_signature(raw_body: bytes, signature: str | None, secret: str | None = None) -> None:
    """Raise ``InvalidHeyGenSignature`` unless signature matches raw bytes.

    HeyGen signs the exact request body with HMAC-SHA256 and sends the hex digest
    in the ``signature`` header. Verify before JSON parsing or re-serialization.
    """
    secret = secret or getattr(settings, "heygen_webhook_secret", "")
    if not secret:
        raise InvalidHeyGenSignature("HeyGen webhook secret is not configured")
    if not signature:
        raise InvalidHeyGenSignature("missing HeyGen signature")
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.strip().lower(), expected):
        raise InvalidHeyGenSignature("invalid HeyGen signature")


def parse_webhook(raw_body: bytes, signature: str | None = None, secret: str | None = None) -> dict[str, Any]:
    """Verify and parse a HeyGen webhook payload."""
    verify_webhook_signature(raw_body, signature, secret)
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HeyGenRequestError("HeyGen webhook body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HeyGenRequestError("HeyGen webhook payload must be an object")
    return payload


def webhook_video(payload: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Extract ``(event_type, video_id, event_data)`` from a webhook payload."""
    event_type = str(payload.get("event_type") or payload.get("type") or "")
    event_data = payload.get("event_data") or payload.get("data") or {}
    if not isinstance(event_data, dict):
        event_data = {}
    video_id = event_data.get("video_id") or payload.get("video_id")
    if not event_type or not video_id:
        raise HeyGenRequestError("HeyGen webhook lacks event_type or event_data.video_id", body=payload)
    return event_type, str(video_id), event_data
