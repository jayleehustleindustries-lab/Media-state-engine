from uuid import UUID
import json
from . import elevenlabs, remotion, heygen, jobs, webhooks
from ..db import transaction
from ..state_machine import advance, IllegalTransition

async def generate_audio(job_id: UUID):
    key = f'{job_id}:audio'
    async with transaction() as conn:
        existing = await jobs.get_key(conn, key)
        if existing:
            return existing['result'] or {'status': 'in_progress', 'idempotency_key': key}
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job: raise LookupError('job not found')
        await jobs.reserve_key(conn, key, job_id, 'audio')
        await advance(conn, job_id, 'audio_generating', {'idempotency_key': key})
    result = await elevenlabs.generate(job['script_text'], str(job_id))
    async with transaction() as conn:
        asset = await conn.fetchrow('INSERT INTO assets(job_id,kind,url,storage_path,meta) VALUES($1,\'audio\',$2,$3,$4::jsonb) RETURNING *', job_id, result.get('url'), result.get('storage_path'), json.dumps(result.get('meta', {})))
        await advance(conn, job_id, 'audio_ready', {'asset_id': str(asset['id'])})
        await conn.execute('UPDATE idempotency_keys SET result=$2::jsonb WHERE key=$1', key, json.dumps({'asset_id': str(asset['id']), 'status': 'audio_ready'}))
        return {'asset_id': str(asset['id']), 'status': 'audio_ready'}

async def render(job_id: UUID):
    key = f'{job_id}:render'
    async with transaction() as conn:
        existing = await jobs.get_key(conn, key)
        if existing:
            return existing['result'] or {'status': 'in_progress', 'idempotency_key': key}
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job: raise LookupError('job not found')
        audio = await conn.fetchrow('SELECT * FROM assets WHERE job_id=$1 AND kind=\'audio\'', job_id)
        await jobs.reserve_key(conn, key, job_id, 'render')
        await advance(conn, job_id, 'rendering', {'idempotency_key': key})
    result = await remotion.render(job, dict(audio) if audio else None)
    async with transaction() as conn:
        asset = await conn.fetchrow('INSERT INTO assets(job_id,kind,url,storage_path,meta) VALUES($1,\'final\',$2,$3,$4::jsonb) RETURNING *', job_id, result.get('url'), result.get('storage_path'), json.dumps(result.get('meta', {})))
        await advance(conn, job_id, 'rendered', {'asset_id': str(asset['id'])})
        await conn.execute('UPDATE idempotency_keys SET result=$2::jsonb WHERE key=$1', key, json.dumps({'asset_id': str(asset['id']), 'status': 'rendered'}))
        response = {'asset_id': str(asset['id']), 'status': 'rendered'}
    await webhooks.delivery(job_id, response)
    return response

async def generate_avatar(job_id: UUID, avatar_id: str | None = None, voice_id: str | None = None):
    """Start a HeyGen-only avatar render; completion arrives through webhook."""
    key = f'{job_id}:heygen-avatar'
    async with transaction() as conn:
        existing = await jobs.get_key(conn, key)
        if existing:
            return existing['result'] or {'status': 'in_progress', 'idempotency_key': key}
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job: raise LookupError('job not found')
        await jobs.reserve_key(conn, key, job_id, 'heygen-avatar')
        await advance(conn, job_id, 'rendering', {'provider': 'heygen', 'idempotency_key': key})
    result = await heygen.create_video(
        job_id=job_id,
        script_text=job['script_text'],
        avatar_id=avatar_id,
        voice_id=voice_id,
        callback_id=f'job:{job_id}',
        idempotency_key=key,
    )
    async with transaction() as conn:
        asset = await conn.fetchrow(
            "INSERT INTO assets(job_id,kind,url,storage_path,meta) VALUES($1,'final',$2,$3,$4::jsonb) RETURNING *",
            job_id, result.video_url, f'heygen/{result.video_id}.mp4', json.dumps({
                'provider': 'heygen', 'video_id': result.video_id, 'status': result.status,
                'thumbnail_url': result.thumbnail_url, 'callback_id': f'job:{job_id}',
            }))
        response = {'asset_id': str(asset['id']), 'video_id': result.video_id, 'status': 'rendering'}
        await conn.execute('UPDATE idempotency_keys SET result=$2::jsonb WHERE key=$1', key, json.dumps(response))
        return response

async def complete_heygen_webhook(event_type: str, video_id: str, event_data: dict):
    """Apply one HeyGen success/failure event transactionally and idempotently."""
    async with transaction() as conn:
        asset = await conn.fetchrow("SELECT * FROM assets WHERE kind='final' AND meta->>'video_id'=$1 FOR UPDATE", video_id)
        if not asset:
            raise LookupError('HeyGen video is not associated with a job')
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', asset['job_id'])
        if job['status'] == 'rendered':
            return {'job_id': str(job['id']), 'asset_id': str(asset['id']), 'video_id': video_id, 'status': 'rendered'}
        if job['status'] == 'failed':
            return {'job_id': str(job['id']), 'asset_id': str(asset['id']), 'video_id': video_id, 'status': 'failed'}
        if event_type.endswith('.fail') or event_type.endswith('.failed'):
            await advance(conn, job['id'], 'failed', {'provider': 'heygen', 'video_id': video_id, 'event_type': event_type})
            await conn.execute("UPDATE assets SET meta = meta || $2::jsonb WHERE id=$1", asset['id'], json.dumps({'status': 'failed', 'provider_payload': event_data}))
            return {'job_id': str(job['id']), 'status': 'failed'}
        url = event_data.get('video_url') or event_data.get('url')
        thumbnail = event_data.get('thumbnail_url')
        await advance(conn, job['id'], 'rendered', {'provider': 'heygen', 'video_id': video_id, 'event_type': event_type})
        await conn.execute("UPDATE assets SET url=COALESCE($2,url), meta = meta || $3::jsonb WHERE id=$1", asset['id'], url, json.dumps({'status': 'completed', 'thumbnail_url': thumbnail, 'provider_payload': event_data}))
        response = {'job_id': str(job['id']), 'asset_id': str(asset['id']), 'video_id': video_id, 'status': 'rendered'}
    await webhooks.delivery(job['id'], response)
    return response
