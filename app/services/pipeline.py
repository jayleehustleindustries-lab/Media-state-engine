from uuid import UUID
import json
from . import elevenlabs, remotion, jobs, webhooks
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
