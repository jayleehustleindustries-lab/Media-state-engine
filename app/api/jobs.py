from uuid import UUID
import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_api_key
from ..config import settings
from ..models import JobCreate, AdvanceRequest, AssetCreate, AudioWebhook, RenderWebhook
from ..services import jobs, pipeline, heygen, outbox
from ..state_machine import IllegalTransition

router = APIRouter()


def out(row):
    return dict(row) if row else None


def _verify_inbound_hmac(raw_body: bytes, signature: str | None, secret: str, name: str) -> None:
    if not secret:
        raise HTTPException(503, f'{name} webhook secret is not configured')
    if not signature:
        raise HTTPException(401, f'missing {name} signature')
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # Accept raw hex or sha256=<hex>
    presented = signature.strip()
    if presented.lower().startswith('sha256='):
        presented = presented.split('=', 1)[1].strip()
    if not hmac.compare_digest(presented.lower(), expected):
        raise HTTPException(401, f'invalid {name} signature')


@router.post('/jobs', status_code=201, dependencies=[Depends(require_api_key)])
async def create(request: JobCreate):
    return out(await jobs.create_job(request.script_text))


@router.get('/jobs/{job_id}', dependencies=[Depends(require_api_key)])
async def get(job_id: UUID):
    row = await jobs.get_job(job_id)
    if not row:
        raise HTTPException(404, 'job not found')
    return out(row)


@router.post('/jobs/{job_id}/advance', dependencies=[Depends(require_api_key)])
async def advance_endpoint(job_id: UUID, request: AdvanceRequest):
    try:
        return out(await jobs.advance_job(job_id, request.to_status, request.payload))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))


@router.post('/jobs/{job_id}/assets', status_code=201, dependencies=[Depends(require_api_key)])
async def asset(job_id: UUID, request: AssetCreate):
    try:
        return out(await jobs.add_asset(job_id, request.kind, request.url, request.storage_path, request.meta))
    except Exception as exc:
        raise HTTPException(409, str(exc))


@router.post('/jobs/{job_id}/generate-audio', dependencies=[Depends(require_api_key)])
async def audio(job_id: UUID):
    try:
        return await pipeline.generate_audio(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (IllegalTransition, RuntimeError) as exc:
        raise HTTPException(409, str(exc))


@router.post('/jobs/{job_id}/render', dependencies=[Depends(require_api_key)])
async def render(job_id: UUID):
    try:
        return await pipeline.render(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (IllegalTransition, RuntimeError) as exc:
        raise HTTPException(409, str(exc))


@router.post('/jobs/{job_id}/generate-avatar', dependencies=[Depends(require_api_key)])
async def avatar(job_id: UUID):
    try:
        return await pipeline.generate_avatar(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (IllegalTransition, RuntimeError, heygen.HeyGenError) as exc:
        raise HTTPException(409, str(exc))


@router.post('/jobs/{job_id}/reconcile', dependencies=[Depends(require_api_key)])
async def reconcile(job_id: UUID):
    """Poll HeyGen get_video for a stuck rendering job and apply terminal state."""
    try:
        return await pipeline.reconcile_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (IllegalTransition, heygen.HeyGenError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))


@router.post('/admin/reconcile-stuck', dependencies=[Depends(require_api_key)])
async def reconcile_stuck(older_than_seconds: int | None = None, limit: int = 50):
    return await pipeline.reconcile_stuck(older_than_seconds=older_than_seconds, limit=limit)


@router.post('/admin/outbox/flush', dependencies=[Depends(require_api_key)])
async def flush_outbox(limit: int = 20):
    return await outbox.flush_outbox(limit=limit)


@router.post('/webhooks/elevenlabs')
async def elevenlabs_webhook(request: Request):
    raw = await request.body()
    _verify_inbound_hmac(
        raw,
        request.headers.get('x-webhook-signature') or request.headers.get('signature'),
        settings.elevenlabs_webhook_secret,
        'ElevenLabs',
    )
    try:
        body = AudioWebhook.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(400, f'invalid payload: {exc}') from exc
    return await jobs.add_asset(body.job_id, 'audio', body.url, body.storage_path, body.meta)


@router.post('/webhooks/remotion')
async def remotion_webhook(request: Request):
    raw = await request.body()
    _verify_inbound_hmac(
        raw,
        request.headers.get('x-webhook-signature') or request.headers.get('signature'),
        settings.remotion_webhook_secret,
        'Remotion',
    )
    try:
        body = RenderWebhook.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(400, f'invalid payload: {exc}') from exc
    return await jobs.add_asset(body.job_id, 'final', body.url, body.storage_path, body.meta)


@router.post('/webhooks/heygen')
async def heygen_webhook(request: Request):
    raw_body = await request.body()
    try:
        payload = heygen.parse_webhook(raw_body, request.headers.get('signature'))
        event_type, video_id, event_data = heygen.webhook_video(payload)
        return await pipeline.complete_heygen_webhook(event_type, video_id, event_data)
    except heygen.InvalidHeyGenSignature as exc:
        raise HTTPException(401, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except heygen.HeyGenError as exc:
        raise HTTPException(400, str(exc))
