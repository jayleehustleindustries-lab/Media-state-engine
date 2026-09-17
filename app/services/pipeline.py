from uuid import UUID
import json
from . import elevenlabs, remotion, heygen, jobs, webhooks, outbox
from ..db import transaction
from ..state_machine import advance, IllegalTransition


async def _fail_step(conn, job_id: UUID, key: str, payload: dict):
    """Mark job failed, clear poisoned idempotency key, emit event."""
    await jobs.clear_key(conn, key)
    try:
        await advance(conn, job_id, 'failed', payload)
    except IllegalTransition:
        # Already terminal or unexpected — still clear key above.
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
        if job and job['status'] != 'failed':
            raise


async def generate_audio(job_id: UUID):
    key = f'{job_id}:audio'
    async with transaction() as conn:
        existing = await jobs.get_key(conn, key)
        if existing:
            return existing['result'] or {'status': 'in_progress', 'idempotency_key': key}
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job:
            raise LookupError('job not found')
        await jobs.reserve_key(conn, key, job_id, 'audio')
        await advance(conn, job_id, 'audio_generating', {'idempotency_key': key})
    try:
        result = await elevenlabs.generate(job['script_text'], str(job_id))
    except Exception as exc:
        async with transaction() as conn:
            await _fail_step(conn, job_id, key, {'step': 'audio', 'error': str(exc)})
        raise
    async with transaction() as conn:
        asset = await conn.fetchrow(
            "INSERT INTO assets(job_id,kind,url,storage_path,meta) VALUES($1,'audio',$2,$3,$4::jsonb) RETURNING *",
            job_id, result.get('url'), result.get('storage_path'), json.dumps(result.get('meta', {})),
        )
        await advance(conn, job_id, 'audio_ready', {'asset_id': str(asset['id'])})
        await conn.execute(
            'UPDATE idempotency_keys SET result=$2::jsonb WHERE key=$1',
            key, json.dumps({'asset_id': str(asset['id']), 'status': 'audio_ready'}),
        )
        return {'asset_id': str(asset['id']), 'status': 'audio_ready'}


async def render(job_id: UUID):
    key = f'{job_id}:render'
    async with transaction() as conn:
        existing = await jobs.get_key(conn, key)
        if existing:
            return existing['result'] or {'status': 'in_progress', 'idempotency_key': key}
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job:
            raise LookupError('job not found')
        audio = await conn.fetchrow("SELECT * FROM assets WHERE job_id=$1 AND kind='audio'", job_id)
        await jobs.reserve_key(conn, key, job_id, 'render')
        await advance(conn, job_id, 'rendering', {'idempotency_key': key})
    try:
        result = await remotion.render(job, dict(audio) if audio else None)
    except Exception as exc:
        async with transaction() as conn:
            await _fail_step(conn, job_id, key, {'step': 'render', 'error': str(exc)})
        raise
    async with transaction() as conn:
        asset = await conn.fetchrow(
            "INSERT INTO assets(job_id,kind,url,storage_path,meta) VALUES($1,'final',$2,$3,$4::jsonb) RETURNING *",
            job_id, result.get('url'), result.get('storage_path'), json.dumps(result.get('meta', {})),
        )
        await advance(conn, job_id, 'rendered', {'asset_id': str(asset['id'])})
        await conn.execute(
            'UPDATE idempotency_keys SET result=$2::jsonb WHERE key=$1',
            key, json.dumps({'asset_id': str(asset['id']), 'status': 'rendered'}),
        )
        response = {'asset_id': str(asset['id']), 'status': 'rendered'}
        await webhooks.enqueue_in_txn(conn, job_id, response)
    await outbox.flush_outbox(limit=10)
    return response


async def generate_avatar(job_id: UUID, avatar_id: str | None = None, voice_id: str | None = None):
    """Start a HeyGen-only avatar render; completion arrives through webhook or reconcile."""
    key = f'{job_id}:heygen-avatar'
    async with transaction() as conn:
        existing = await jobs.get_key(conn, key)
        if existing:
            return existing['result'] or {'status': 'in_progress', 'idempotency_key': key}
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job:
            raise LookupError('job not found')
        await jobs.reserve_key(conn, key, job_id, 'heygen-avatar')
        await advance(conn, job_id, 'rendering', {'provider': 'heygen', 'idempotency_key': key})
    try:
        result = await heygen.create_video(
            job_id=job_id,
            script_text=job['script_text'],
            avatar_id=avatar_id,
            voice_id=voice_id,
            callback_id=f'job:{job_id}',
            idempotency_key=key,
        )
    except Exception as exc:
        async with transaction() as conn:
            await _fail_step(conn, job_id, key, {
                'step': 'heygen-avatar', 'provider': 'heygen', 'error': str(exc),
            })
        raise
    async with transaction() as conn:
        # In-progress HeyGen work is kind=video; kind=final only when delivered.
        asset = await conn.fetchrow(
            """
            INSERT INTO assets(job_id,kind,url,storage_path,meta)
            VALUES($1,'video',$2,$3,$4::jsonb)
            RETURNING *
            """,
            job_id,
            result.video_url,
            f'heygen/{result.video_id}.mp4',
            json.dumps({
                'provider': 'heygen',
                'video_id': result.video_id,
                'status': result.status,
                'thumbnail_url': result.thumbnail_url,
                'callback_id': f'job:{job_id}',
            }),
        )
        response = {
            'asset_id': str(asset['id']),
            'video_id': result.video_id,
            'status': 'rendering',
            'kind': 'video',
        }
        await conn.execute(
            'UPDATE idempotency_keys SET result=$2::jsonb WHERE key=$1',
            key, json.dumps(response),
        )
        return response


async def complete_heygen_webhook(event_type: str, video_id: str, event_data: dict):
    """Apply one HeyGen success/failure event transactionally and idempotently."""
    async with transaction() as conn:
        asset = await conn.fetchrow(
            """
            SELECT * FROM assets
            WHERE kind IN ('video','final') AND meta->>'video_id'=$1
            ORDER BY CASE kind WHEN 'video' THEN 0 ELSE 1 END
            FOR UPDATE
            """,
            video_id,
        )
        if not asset:
            raise LookupError('HeyGen video is not associated with a job')
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', asset['job_id'])
        if job['status'] == 'rendered':
            return {
                'job_id': str(job['id']), 'asset_id': str(asset['id']),
                'video_id': video_id, 'status': 'rendered',
            }
        if job['status'] == 'failed':
            return {
                'job_id': str(job['id']), 'asset_id': str(asset['id']),
                'video_id': video_id, 'status': 'failed',
            }
        key = f"{job['id']}:heygen-avatar"
        if event_type.endswith('.fail') or event_type.endswith('.failed'):
            await jobs.clear_key(conn, key)
            await advance(conn, job['id'], 'failed', {
                'provider': 'heygen', 'video_id': video_id, 'event_type': event_type,
            })
            await conn.execute(
                "UPDATE assets SET meta = meta || $2::jsonb WHERE id=$1",
                asset['id'], json.dumps({'status': 'failed', 'provider_payload': event_data}),
            )
            return {'job_id': str(job['id']), 'status': 'failed'}

        url = event_data.get('video_url') or event_data.get('url')
        thumbnail = event_data.get('thumbnail_url')
        await advance(conn, job['id'], 'rendered', {
            'provider': 'heygen', 'video_id': video_id, 'event_type': event_type,
        })
        # Keep in-progress video asset; promote delivered artifact as kind=final.
        await conn.execute(
            "UPDATE assets SET meta = meta || $2::jsonb WHERE id=$1",
            asset['id'],
            json.dumps({'status': 'completed', 'thumbnail_url': thumbnail, 'provider_payload': event_data}),
        )
        final = await conn.fetchrow(
            """
            INSERT INTO assets(job_id,kind,url,storage_path,meta)
            VALUES($1,'final',$2,$3,$4::jsonb)
            ON CONFLICT (job_id, kind) DO UPDATE
              SET url = COALESCE(EXCLUDED.url, assets.url),
                  meta = assets.meta || EXCLUDED.meta
            RETURNING *
            """,
            job['id'],
            url,
            asset['storage_path'] or f'heygen/{video_id}.mp4',
            json.dumps({
                'provider': 'heygen',
                'video_id': video_id,
                'status': 'completed',
                'thumbnail_url': thumbnail,
                'provider_payload': event_data,
            }),
        )
        response = {
            'job_id': str(job['id']),
            'asset_id': str(final['id']),
            'video_id': video_id,
            'status': 'rendered',
            'kind': 'final',
        }
        await webhooks.enqueue_in_txn(conn, job['id'], response)
    await outbox.flush_outbox(limit=10)
    return response


async def reconcile_job(job_id: UUID) -> dict:
    """Poll HeyGen for a job stuck in rendering; apply terminal success/failure."""
    async with transaction() as conn:
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job:
            raise LookupError('job not found')
        if job['status'] not in ('rendering', 'audio_generating'):
            return {
                'job_id': str(job_id),
                'status': job['status'],
                'reconciled': False,
                'reason': 'job not in a stuck-eligible status',
            }
        asset = await conn.fetchrow(
            """
            SELECT * FROM assets
            WHERE job_id=$1 AND kind='video' AND meta->>'provider'='heygen'
            """,
            job_id,
        )
        meta = asset['meta'] if asset else None
        if isinstance(meta, str):
            meta = json.loads(meta)
        meta = meta or {}
        if not asset or not meta.get('video_id'):
            # No provider handle — if stuck in audio_generating with no progress, fail.
            if job['status'] == 'audio_generating':
                key = f'{job_id}:audio'
                await _fail_step(conn, job_id, key, {
                    'step': 'reconcile', 'error': 'stuck in audio_generating with no recoverable provider handle',
                })
                return {'job_id': str(job_id), 'status': 'failed', 'reconciled': True}
            return {
                'job_id': str(job_id),
                'status': job['status'],
                'reconciled': False,
                'reason': 'no heygen video asset to reconcile',
            }
        video_id = meta['video_id']

    video = await heygen.get_video(video_id)
    status_lower = (video.status or '').lower()
    terminal_fail = status_lower in ('failed', 'fail', 'error', 'canceled', 'cancelled')
    terminal_ok = status_lower in ('completed', 'complete', 'success', 'succeeded', 'ready')

    if terminal_fail:
        return await complete_heygen_webhook(
            'avatar_video.fail',
            video_id,
            {'video_id': video_id, 'status': video.status, 'raw': dict(video.raw or {})},
        )
    if terminal_ok or video.video_url:
        return await complete_heygen_webhook(
            'avatar_video.success',
            video_id,
            {
                'video_id': video_id,
                'video_url': video.video_url,
                'thumbnail_url': video.thumbnail_url,
                'status': video.status,
            },
        )
    return {
        'job_id': str(job_id),
        'status': job['status'],
        'video_id': video_id,
        'provider_status': video.status,
        'reconciled': False,
        'reason': 'provider still in progress',
    }


async def reconcile_stuck(*, older_than_seconds: int | None = None, limit: int = 50) -> dict:
    """Find stuck rendering/audio_generating jobs and reconcile each."""
    from ..config import settings

    seconds = older_than_seconds if older_than_seconds is not None else settings.stuck_job_seconds
    async with transaction() as conn:
        rows = await conn.fetch(
            """
            SELECT id FROM jobs
            WHERE status IN ('rendering', 'audio_generating')
              AND updated_at < now() - ($1 || ' seconds')::interval
            ORDER BY updated_at
            LIMIT $2
            """,
            str(seconds),
            limit,
        )
    results = []
    for row in rows:
        try:
            results.append(await reconcile_job(row['id']))
        except Exception as exc:  # noqa: BLE001
            results.append({'job_id': str(row['id']), 'error': str(exc), 'reconciled': False})
    return {'checked': len(rows), 'results': results}
