from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from ..models import JobCreate, AdvanceRequest, AssetCreate, AudioWebhook, RenderWebhook
from ..services import jobs, pipeline, heygen
from ..state_machine import IllegalTransition

router = APIRouter()

def out(row): return dict(row) if row else None

@router.post('/jobs', status_code=201)
async def create(request: JobCreate): return out(await jobs.create_job(request.script_text))

@router.get('/jobs/{job_id}')
async def get(job_id: UUID):
    row = await jobs.get_job(job_id)
    if not row: raise HTTPException(404, 'job not found')
    return out(row)

@router.post('/jobs/{job_id}/advance')
async def advance(job_id: UUID, request: AdvanceRequest):
    try: return out(await jobs.advance_job(job_id, request.to_status, request.payload))
    except LookupError as exc: raise HTTPException(404, str(exc))
    except IllegalTransition as exc: raise HTTPException(409, str(exc))

@router.post('/jobs/{job_id}/assets', status_code=201)
async def asset(job_id: UUID, request: AssetCreate):
    try: return out(await jobs.add_asset(job_id, request.kind, request.url, request.storage_path, request.meta))
    except Exception as exc: raise HTTPException(409, str(exc))

@router.post('/jobs/{job_id}/generate-audio')
async def audio(job_id: UUID):
    try: return await pipeline.generate_audio(job_id)
    except LookupError as exc: raise HTTPException(404, str(exc))
    except (IllegalTransition, RuntimeError) as exc: raise HTTPException(409, str(exc))

@router.post('/jobs/{job_id}/render')
async def render(job_id: UUID):
    try: return await pipeline.render(job_id)
    except LookupError as exc: raise HTTPException(404, str(exc))
    except (IllegalTransition, RuntimeError) as exc: raise HTTPException(409, str(exc))

@router.post('/jobs/{job_id}/generate-avatar')
async def avatar(job_id: UUID):
    try: return await pipeline.generate_avatar(job_id)
    except LookupError as exc: raise HTTPException(404, str(exc))
    except (IllegalTransition, RuntimeError, heygen.HeyGenError) as exc: raise HTTPException(409, str(exc))

@router.post('/webhooks/elevenlabs')
async def elevenlabs_webhook(request: AudioWebhook):
    return await jobs.add_asset(request.job_id, 'audio', request.url, request.storage_path, request.meta)

@router.post('/webhooks/remotion')
async def remotion_webhook(request: RenderWebhook):
    return await jobs.add_asset(request.job_id, 'final', request.url, request.storage_path, request.meta)

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
