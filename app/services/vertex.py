"""Google Vertex AI (Veo) video generation integration.

Structurally mirrors ``heygen.py``: config check -> typed exceptions ->
dataclass result -> async httpx calls -> caller reserves the DB idempotency
key before calling in, and persists the returned provider handle on the
job's asset metadata. It differs from HeyGen in the two ways this provider
is fundamentally different:

  1. **Auth is OAuth2, not a static API key.** Vertex AI authenticates with
     a bearer access token minted from Application Default Credentials or a
     service-account key (via the ``google-auth`` library), refreshed on
     every call rather than a header set once. There is no equivalent of
     HeyGen's ``X-Api-Key``.
  2. **Completion is polling-only.** Generation is a long-running operation
     (LRO): ``predictLongRunning`` starts it, ``fetchPredictOperation``
     polls it. Unlike HeyGen (which supports a webhook callback, with
     reconcile/poll only as a stuck-job safety net), Vertex has **no**
     webhook/callback option for video generation — Google's own SDK
     raises "webhook_config parameter is only supported in Gemini Developer
     API mode, not in ... Vertex AI" if you try. So for this provider,
     polling via ``get_clip_operation`` (called from
     ``pipeline.reconcile_vertex_clips``) is the *only* completion path,
     not a fallback.

PROVENANCE — verified vs. inferred
-----------------------------------
Live docs.cloud.google.com was unreachable from this environment (network
egress policy blocks that host; confirmed via direct curl and WebFetch,
both returning EGRESS_BLOCKED / CONNECT tunnel failures). Instead this was
verified by pip-installing Google's own official ``google-genai`` Python
SDK (v2.25.0, published to PyPI, not from memory) into a scratch venv and
reading its source directly — the SDK's internal vertex-mode request/response
converters are Google's own authoritative mapping of the wire format:

  - CONFIRMED (from google-genai source, not guessed):
    - Endpoint shape: ``POST https://{location}-aiplatform.googleapis.com/v1/
      projects/{project}/locations/{location}/publishers/google/models/
      {model}:predictLongRunning`` to start; ``.../{model}:fetchPredictOperation``
      (POST, body ``{"operationName": "<name>"}``) to poll.
    - Request body: ``{"instances": [{"prompt": ..., "referenceImages": [...]}],
      "parameters": {"aspectRatio": ..., "durationSeconds": ..., "sampleCount": ...}}``.
    - Reference image shape: ``{"image": {"bytesBase64Encoded": ..., "mimeType": ...},
      "referenceType": "ASSET"}`` — ``ASSET`` is Google's own enum value for
      "a reference image that provides ... a character" (i.e. identity lock),
      which lines up with what the Make.com blueprint was doing under a
      different field name.
    - predictLongRunning response: ``{"name": "projects/.../operations/...", "done": bool}``.
    - fetchPredictOperation response when done: ``{"done": true,
      "response": {"videos": [{"bytesBase64Encoded": ..., "mimeType": ...}
      or {"gcsUri": ...}]}}`` — this confirms the Make module's
      ``videos[1].videoData`` (1-indexed) is that same ``videos[0].bytesBase64Encoded``.
    - Auth: ``google.auth.default()`` (Application Default Credentials) or
      ``google.oauth2.service_account.Credentials``, refreshed via
      ``google.auth.transport.requests.Request()``, sent as
      ``Authorization: Bearer {token}``. No static API key.
  - NOT independently confirmed (best-effort from SDK test fixtures /
    reasonable defaults, flagged so a real call can correct it):
    - Exact GA model resource id. SDK test fixtures show
      ``veo-3.1-generate-preview`` / ``veo-3.0-generate-001`` / ``veo-2.0-generate-001``
      as real, currently-used ids; this module defaults to
      ``veo-3.0-generate-001`` via ``settings.vertex_model_id`` (override freely —
      no live credentials exist in this environment to test an actual call).
    - ``sampleCount``/duration bounds and the exact 6-reference-image cap are
      carried over from the Make blueprint's observed behavior, not from the
      SDK source (the SDK does not hardcode a reference-image limit itself).
"""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

import httpx

from ..config import settings

_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
_MAX_REFERENCE_IMAGES = 6


class VertexError(RuntimeError):
    """Base class for expected Vertex AI integration errors."""


class VertexNotConfigured(VertexError):
    pass


class VertexAuthenticationError(VertexError):
    pass


class VertexRequestError(VertexError):
    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True)
class VertexOperation:
    """One clip's Veo long-running operation, at whatever point it was read."""

    operation_name: str
    done: bool = False
    video_bytes_b64: str | None = None
    video_gcs_uri: str | None = None
    mime_type: str | None = None
    error: str | None = None
    raw: Mapping[str, Any] | None = None


def _require_config() -> tuple[str, str, str]:
    project_id = (getattr(settings, "vertex_project_id", "") or "").strip()
    location = (getattr(settings, "vertex_location", "") or "us-central1").strip() or "us-central1"
    model_id = (getattr(settings, "vertex_model_id", "") or "veo-3.0-generate-001").strip() or "veo-3.0-generate-001"
    if not project_id:
        raise VertexNotConfigured("Vertex AI is not configured: set VERTEX_PROJECT_ID")
    return project_id, location, model_id


def _model_resource(project_id: str, location: str, model_id: str) -> str:
    return f"projects/{project_id}/locations/{location}/publishers/google/models/{model_id}"


def _base_url(location: str) -> str:
    return f"https://{location}-aiplatform.googleapis.com/v1"


def _load_credentials():
    """Blocking: build google-auth credentials from whichever source is configured.

    Precedence: inline service-account JSON -> service-account key file path ->
    ambient Application Default Credentials.
    """
    try:
        import google.auth
        from google.oauth2 import service_account
    except ImportError as exc:
        raise VertexNotConfigured(
            "google-auth is not installed: pip install google-auth"
        ) from exc

    sa_json = (getattr(settings, "vertex_service_account_json", "") or "").strip()
    sa_path = (getattr(settings, "google_application_credentials", "") or "").strip()
    try:
        if sa_json:
            info = json.loads(sa_json)
            return service_account.Credentials.from_service_account_info(info, scopes=list(_SCOPES))
        if sa_path:
            return service_account.Credentials.from_service_account_file(sa_path, scopes=list(_SCOPES))
        credentials, _project = google.auth.default(scopes=list(_SCOPES))
        return credentials
    except VertexError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise VertexNotConfigured(
            "Vertex AI credentials not found: set VERTEX_SERVICE_ACCOUNT_JSON, "
            f"GOOGLE_APPLICATION_CREDENTIALS, or configure ambient ADC ({exc})"
        ) from exc


def _refresh_token(credentials) -> str:
    """Blocking: mint/refresh an OAuth2 access token from loaded credentials."""
    from google.auth.transport.requests import Request

    try:
        credentials.refresh(Request())
    except Exception as exc:  # noqa: BLE001
        raise VertexAuthenticationError(f"failed to refresh Google access token: {exc}") from exc
    if not credentials.token:
        raise VertexAuthenticationError("Google credentials did not yield an access token")
    return str(credentials.token)


def _mint_access_token_sync() -> str:
    return _refresh_token(_load_credentials())


async def _access_token() -> str:
    """Mint a fresh bearer token off the event loop (blocking google-auth I/O)."""
    return await asyncio.to_thread(_mint_access_token_sync)


async def _headers() -> dict[str, str]:
    token = await _access_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


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
        raise VertexAuthenticationError(
            f"Vertex AI rejected the request credentials (HTTP {response.status_code})"
        )
    message = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message")
    raise VertexRequestError(
        message or f"Vertex AI request failed with HTTP {response.status_code}",
        status_code=response.status_code,
        body=body,
    )


def _reference_images_payload(reference_images: list[dict] | None) -> list[dict] | None:
    """Convert ``[{'data': b64-or-bytes, 'mime_type': 'image/png'}]`` (up to 6,
    per the Make-observed identity-lock cap) into Vertex's ``referenceImages``
    shape, tagged ``ASSET`` (Google's own enum for a character/identity
    reference — see PROVENANCE above)."""
    if not reference_images:
        return None
    payload: list[dict[str, Any]] = []
    for ref in reference_images[:_MAX_REFERENCE_IMAGES]:
        data = ref.get("data")
        if isinstance(data, (bytes, bytearray)):
            data = base64.b64encode(data).decode("ascii")
        if not data:
            continue
        payload.append(
            {
                "image": {"bytesBase64Encoded": data, "mimeType": ref.get("mime_type", "image/png")},
                "referenceType": "ASSET",
            }
        )
    return payload or None


async def start_clip_generation(
    *,
    job_id: UUID | str,
    prompt: str,
    clip_index: int,
    reference_images: list[dict] | None = None,
    aspect_ratio: str | None = None,
    duration_seconds: int | None = None,
) -> VertexOperation:
    """Start one Veo clip generation (``predictLongRunning``).

    Returns immediately with the LRO handle; the clip is NOT ready yet.
    Completion must be polled via ``get_clip_operation`` — see module
    docstring: this provider has no webhook/callback path at all.
    """
    project_id, location, model_id = _require_config()
    prompt = (prompt or "").strip()
    if not prompt:
        raise VertexRequestError(f"prompt is required for clip {clip_index}")

    instance: dict[str, Any] = {"prompt": prompt}
    refs = _reference_images_payload(reference_images)
    if refs:
        instance["referenceImages"] = refs

    parameters: dict[str, Any] = {
        "aspectRatio": aspect_ratio or getattr(settings, "vertex_aspect_ratio", "9:16"),
        "sampleCount": 1,
    }
    dur = duration_seconds if duration_seconds is not None else getattr(settings, "vertex_clip_duration_seconds", 8)
    if dur:
        parameters["durationSeconds"] = int(dur)

    model_resource = _model_resource(project_id, location, model_id)
    url = f"{_base_url(location)}/{model_resource}:predictLongRunning"
    headers = await _headers()
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            url, headers=headers, json={"instances": [instance], "parameters": parameters}
        )
    _raise_for_provider(response)
    data = response.json() if response.content else {}
    operation_name = data.get("name")
    if not operation_name:
        raise VertexRequestError("Vertex AI response did not include an operation name", body=data)
    return VertexOperation(operation_name=str(operation_name), done=bool(data.get("done", False)), raw=data)


async def get_clip_operation(operation_name: str) -> VertexOperation:
    """Poll ``fetchPredictOperation`` for one clip's LRO.

    Mirrors ``heygen.get_video`` in shape, but for Vertex this poll is the
    *only* way a clip's completion is ever observed (see module docstring).
    """
    project_id, location, model_id = _require_config()
    model_resource = _model_resource(project_id, location, model_id)
    url = f"{_base_url(location)}/{model_resource}:fetchPredictOperation"
    headers = await _headers()
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, headers=headers, json={"operationName": operation_name})
    _raise_for_provider(response)
    data = response.json() if response.content else {}
    done = bool(data.get("done", False))
    if not done:
        return VertexOperation(operation_name=operation_name, done=False, raw=data)

    error = data.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        return VertexOperation(
            operation_name=operation_name, done=True, error=message or "operation failed", raw=data
        )

    videos = ((data.get("response") or {}).get("videos")) or []
    if not videos:
        return VertexOperation(
            operation_name=operation_name,
            done=True,
            error="operation completed with no videos in response",
            raw=data,
        )
    video = videos[0]
    return VertexOperation(
        operation_name=operation_name,
        done=True,
        video_bytes_b64=video.get("bytesBase64Encoded"),
        video_gcs_uri=video.get("gcsUri"),
        mime_type=video.get("mimeType", "video/mp4"),
        raw=data,
    )
