from uuid import UUID
import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_api_key
from ..config import settings
from ..models import JobCreate, AdvanceRequest, AssetCreate, AudioWebhook, RenderWebhook, ApproveRequest
from ..services import jobs, pipeline, heygen, outbox, queue
from ..state_machine import IllegalTransition

router = APIRouter()



def out(row):
    if not row:
        return None
    d = dict(row)
    for k in ('script', 'meta'):
        if k in d and hasattr(d[k], 'keys') is False and isinstance(d[k], str):
            import json
            d[k] = json.loads(d[k])
    for k, v in list(d.items()):
        if hasattr(v, 'isoformat'):
            d[k] = v.isoformat()
        elif k in ('id',) and v is not None:
            d[k] = str(v)
    return d


def _queued(work: dict) -> dict:
    return {
        'work_id': int(work['id']),
        'job_id': str(work['job_id']),
        'step': work['step'],
        'status': work['status'],
        'job_status': work.get('job_status'),
        'attempts': int(work.get('attempts') or 0),
        'queued': work['status'] in ('pending', 'running'),
    }


class AvatarEnqueueBody(BaseModel):
    avatar_id: str | None = None
    voice_id: str | None = None
    include_horizontal: bool | None = None


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
    try:
        row = await jobs.create_job(
            request.script_text,
            topic=request.topic,
            duration_target_seconds=request.duration_target_seconds,
            cta=request.cta,
            include_horizontal=request.include_horizontal,
            platforms=request.platforms,
            auto_script_ready=request.auto_script_ready,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return out(row)


@router.get('/jobs/{job_id}', dependencies=[Depends(require_api_key)])
async def get(job_id: UUID):
    row = await jobs.get_job(job_id)
    if not row:
        raise HTTPException(404, 'job not found')
    return out(row)



@router.get('/jobs/{job_id}/detail', dependencies=[Depends(require_api_key)])
async def detail(job_id: UUID):
    """Full job detail: assets, events, status history, work queue, outbox, script, captions."""
    data = await jobs.get_job_detail(job_id)
    if not data:
        raise HTTPException(404, 'job not found')
    return data


@router.get('/work/{work_id}', dependencies=[Depends(require_api_key)])
async def get_work(work_id: int):
    row = await queue.get_work(work_id)
    if not row:
        raise HTTPException(404, 'work item not found')
    return queue.serialize_work(row)


@router.post('/jobs/{job_id}/advance', dependencies=[Depends(require_api_key)])
async def advance_endpoint(job_id: UUID, request: AdvanceRequest):
    try:
        return out(await jobs.advance_job(job_id, request.to_status, request.payload))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (IllegalTransition, ValueError) as exc:
        raise HTTPException(409, str(exc))


@router.post('/jobs/{job_id}/approve', dependencies=[Depends(require_api_key)])
async def approve_endpoint(job_id: UUID, body: ApproveRequest | None = None):
    """Explicit human approval gate. Required before any distribution.

    Flips staged → approved and may enqueue the Phase-3 distribute stub.
    Does NOT publish to TikTok / Reels / Shorts.
    """
    body = body or ApproveRequest()
    try:
        return await jobs.approve_job(
            job_id,
            approved_by=body.approved_by,
            enqueue_distribute=body.enqueue_distribute,
            note=body.note,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (IllegalTransition, ValueError) as exc:
        raise HTTPException(409, str(exc))


@router.post('/jobs/{job_id}/assets', status_code=201, dependencies=[Depends(require_api_key)])
async def asset(job_id: UUID, request: AssetCreate):
    try:
        return out(await jobs.add_asset(job_id, request.kind, request.url, request.storage_path, request.meta))
    except Exception as exc:
        raise HTTPException(409, str(exc))


@router.post('/jobs/{job_id}/generate-audio', status_code=202, dependencies=[Depends(require_api_key)])
async def audio(job_id: UUID):
    """Enqueue ElevenLabs audio generation; returns immediately with work_id."""
    try:
        work = await queue.enqueue_step(job_id, 'generate_audio')
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return _queued(work)


@router.post('/jobs/{job_id}/render', status_code=202, dependencies=[Depends(require_api_key)])
async def render(job_id: UUID):
    """Enqueue Remotion render; returns immediately with work_id."""
    try:
        work = await queue.enqueue_step(job_id, 'render')
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return _queued(work)


@router.post('/jobs/{job_id}/generate-avatar', status_code=202, dependencies=[Depends(require_api_key)])
async def avatar(job_id: UUID, body: AvatarEnqueueBody | None = None):
    """Enqueue HeyGen Direct Video; completion via callback_url webhook (or reconcile)."""
    payload = {}
    if body:
        if body.avatar_id:
            payload['avatar_id'] = body.avatar_id
        if body.voice_id:
            payload['voice_id'] = body.voice_id
        if body.include_horizontal is not None:
            payload['include_horizontal'] = body.include_horizontal
    try:
        work = await queue.enqueue_step(job_id, 'generate_avatar', payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return _queued(work)


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


@router.post('/admin/worker/tick', dependencies=[Depends(require_api_key)])
async def worker_tick(also_reconcile: bool = False):
    """Run one worker cycle in-process (tests / single-box ops without a separate process)."""
    from ..worker import tick
    return await tick(also_reconcile=also_reconcile)


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
