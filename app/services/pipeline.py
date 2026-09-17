from uuid import UUID
import json
from . import elevenlabs, remotion, heygen, jobs, webhooks, outbox
from .image_gate import assert_pass_for_heygen, GateRefuse
from ..db import transaction
from ..state_machine import advance, IllegalTransition
from ..config import settings


def _job_script_text(job) -> str:
    """Prefer structured full_text; fall back to script_text column."""
    script = job.get('script') if hasattr(job, 'get') else None
    if script is None and 'script' in getattr(job, 'keys', lambda: [])():
        script = job['script']
    if isinstance(script, str):
        script = json.loads(script)
    if isinstance(script, dict) and script.get('full_text'):
        return script['full_text']
    return job['script_text']


def _job_meta(job) -> dict:
    meta = job.get('meta') if hasattr(job, 'get') else job['meta'] if 'meta' in job.keys() else {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    return dict(meta or {})


async def _fail_step(conn, job_id: UUID, key: str, payload: dict):
    """Mark job failed, clear poisoned idempotency key, emit event."""
    await jobs.clear_key(conn, key)
    try:
        await advance(conn, job_id, 'failed', payload)
        # Best-effort counter (same connection / after advance)
        try:
            await conn.execute(
                """
                INSERT INTO metric_counters(name, value, labels, updated_at)
                VALUES('jobs_failed', 1, $1::jsonb, now())
                ON CONFLICT (name) DO UPDATE
                  SET value = metric_counters.value + 1, updated_at = now()
                """,
                json.dumps({'step': (payload or {}).get('step')}),
            )
        except Exception:
            pass
    except IllegalTransition:
        # Already terminal or unexpected — still clear key above.
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
        if job and job['status'] != 'failed':
            raise


async def _stage_for_approval(conn, job_id: UUID, response: dict) -> dict:
    """Advance rendered → staged and enqueue review notification. NEVER publishes."""
    staged_payload = {
        **response,
        'status': 'staged',
        'awaiting_approval': True,
        'auto_publish': False,
        'public_post': False,
        'message': 'Content staged for human approval — no public post',
    }
    await advance(conn, job_id, 'staged', {
        'via': 'render_complete',
        'awaiting_approval': True,
        'auto_publish': False,
    })
    await webhooks.enqueue_in_txn(conn, job_id, staged_payload)
    response = {**response, 'status': 'staged', 'awaiting_approval': True}
    return response


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
        result = await elevenlabs.generate(_job_script_text(job), str(job_id))
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
            job_id, result.get('url'), result.get('storage_path'),
            json.dumps({**(result.get('meta') or {}), 'aspect': '9:16', 'role': 'primary'}),
        )
        await advance(conn, job_id, 'rendered', {'asset_id': str(asset['id'])})
        await conn.execute(
            'UPDATE idempotency_keys SET result=$2::jsonb WHERE key=$1',
            key, json.dumps({'asset_id': str(asset['id']), 'status': 'rendered'}),
        )
        response = {'asset_id': str(asset['id']), 'status': 'rendered', 'kind': 'final'}
        response = await _stage_for_approval(conn, job_id, response)
    await outbox.flush_outbox(limit=10)
    try:
        from . import metrics
        await metrics.incr('jobs_rendered')
        await metrics.incr('jobs_staged')
        await metrics.record_job_cost(job_id, metrics.estimate_cost(heygen_paid_calls=0, used_remotion=True))
    except Exception:
        pass
    return response


async def generate_avatar(job_id: UUID, avatar_id: str | None = None, voice_id: str | None = None,
                          include_horizontal: bool | None = None):
    """Start a HeyGen-only avatar render; completion arrives through webhook or reconcile.

    Dual-format cost guardrail:
      - Primary is always 9:16 (one HeyGen call).
      - Horizontal 16:9 paid second call ONLY if job meta opted in AND
        settings.heygen_allow_dual_format is true.
      - Otherwise horizontal stays derived/staged with no second billable call.
    """
    key = f'{job_id}:heygen-avatar'
    async with transaction() as conn:
        existing = await jobs.get_key(conn, key)
        if existing:
            return existing['result'] or {'status': 'in_progress', 'idempotency_key': key}
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job:
            raise LookupError('job not found')
        meta = _job_meta(job)
        formats = dict(meta.get('formats') or {})
        horizontal = dict(formats.get('horizontal') or {})
        want_h = include_horizontal if include_horizontal is not None else bool(
            formats.get('include_horizontal') or horizontal.get('mode') in ('heygen_paid', 'derived')
            and horizontal.get('status') == 'requested'
        )
        # Re-evaluate paid dual at render time (env may have changed)
        paid_dual = bool(want_h) and bool(settings.heygen_allow_dual_format)
        if want_h and not paid_dual:
            horizontal = {
                **horizontal,
                'aspect': '16:9',
                'width': 1920,
                'height': 1080,
                'mode': 'derived',
                'status': 'staged',
                'heygen_paid': False,
                'note': (
                    'Horizontal staged without second HeyGen call (cost guardrail). '
                    'Enable HEYGEN_ALLOW_DUAL_FORMAT + include_horizontal for paid dual.'
                ),
            }
            formats['horizontal'] = horizontal
            formats['include_horizontal'] = True
            meta['formats'] = formats
            await conn.execute(
                'UPDATE jobs SET meta=$2::jsonb, updated_at=now() WHERE id=$1',
                job_id, json.dumps(meta),
            )
        # IMAGE GATE: refuse HeyGen unless pass score bound to current ref hash
        # (same transaction as idempotency reserve — no TOCTOU)
        try:
            gate_score = await assert_pass_for_heygen(conn, job_id)
        except GateRefuse:
            raise
        await jobs.reserve_key(conn, key, job_id, 'heygen-avatar')
        await advance(conn, job_id, 'rendering', {
            'provider': 'heygen',
            'idempotency_key': key,
            'aspect': '9:16',
            'horizontal_paid': paid_dual,
            'image_score_id': str(gate_score.get('id') or gate_score.get('image_score_id') or ''),
            'image_gate': 'pass',
        })
    script_text = _job_script_text(job)
    try:
        result = await heygen.create_video(
            job_id=job_id,
            script_text=script_text,
            avatar_id=avatar_id,
            voice_id=voice_id,
            dimension=(1080, 1920),
            callback_id=f'job:{job_id}',
            idempotency_key=key,
        )
    except Exception as exc:
        async with transaction() as conn:
            await _fail_step(conn, job_id, key, {
                'step': 'heygen-avatar', 'provider': 'heygen', 'error': str(exc),
            })
        raise

    horizontal_result = None
    if paid_dual:
        h_key = f'{job_id}:heygen-avatar-h'
        existing_h = None
        try:
            async with transaction() as conn:
                existing_h = await jobs.get_key(conn, h_key)
                if not existing_h:
                    # Dual-H: re-assert gate in SAME txn as horizontal reserve
                    # (bind same pass score / ref hash — no skip path)
                    gate_score_h = await assert_pass_for_heygen(conn, job_id)
                    await jobs.reserve_key(conn, h_key, job_id, 'heygen-avatar-h')
                    await conn.execute(
                        """
                        UPDATE jobs
                           SET meta = COALESCE(meta, '{}'::jsonb) || $2::jsonb,
                               updated_at = now()
                         WHERE id = $1
                        """,
                        job_id,
                        json.dumps({
                            'formats': {
                                'horizontal': {
                                    'image_gate': 'pass',
                                    'image_score_id': str(
                                        gate_score_h.get('id')
                                        or gate_score_h.get('image_score_id')
                                        or ''
                                    ),
                                    'image_ref_content_hash': gate_score_h.get(
                                        'ref_content_hash'
                                    ),
                                }
                            }
                        }),
                    )
            if not existing_h:
                horizontal_result = await heygen.create_video(
                    job_id=job_id,
                    script_text=script_text,
                    avatar_id=avatar_id,
                    voice_id=voice_id,
                    dimension=(1920, 1080),
                    callback_id=f'job:{job_id}:h',
                    idempotency_key=h_key,
                )
        except Exception as exc:
            # Primary already started — record horizontal failure in meta; do not fail whole job
            async with transaction() as conn:
                await conn.execute(
                    """
                    UPDATE jobs SET meta = COALESCE(meta, '{}'::jsonb) || $2::jsonb, updated_at=now()
                    WHERE id=$1
                    """,
                    job_id,
                    json.dumps({
                        'formats': {
                            'horizontal': {
                                'mode': 'heygen_paid',
                                'status': 'failed',
                                'heygen_paid': True,
                                'error': str(exc),
                            }
                        }
                    }),
                )
                await jobs.clear_key(conn, h_key)

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
                'aspect': '9:16',
                'role': 'primary',
            }),
        )
        if horizontal_result is not None:
            await conn.fetchrow(
                """
                INSERT INTO assets(job_id,kind,url,storage_path,meta)
                VALUES($1,'video_h',$2,$3,$4::jsonb)
                ON CONFLICT (job_id, kind) DO UPDATE
                  SET url = COALESCE(EXCLUDED.url, assets.url),
                      meta = assets.meta || EXCLUDED.meta
                RETURNING *
                """,
                job_id,
                horizontal_result.video_url,
                f'heygen/{horizontal_result.video_id}.mp4',
                json.dumps({
                    'provider': 'heygen',
                    'video_id': horizontal_result.video_id,
                    'status': horizontal_result.status,
                    'thumbnail_url': horizontal_result.thumbnail_url,
                    'callback_id': f'job:{job_id}:h',
                    'aspect': '16:9',
                    'role': 'horizontal',
                    'heygen_paid': True,
                }),
            )
            await conn.execute(
                'UPDATE idempotency_keys SET result=$2::jsonb WHERE key=$1',
                f'{job_id}:heygen-avatar-h',
                json.dumps({'video_id': horizontal_result.video_id, 'status': 'rendering', 'kind': 'video_h'}),
            )
            await conn.execute(
                """
                UPDATE jobs SET meta = jsonb_set(
                  COALESCE(meta, '{}'::jsonb),
                  '{formats,horizontal}',
                  $2::jsonb,
                  true
                ), updated_at=now() WHERE id=$1
                """,
                job_id,
                json.dumps({
                    'aspect': '16:9',
                    'width': 1920,
                    'height': 1080,
                    'mode': 'heygen_paid',
                    'status': 'rendering',
                    'heygen_paid': True,
                    'video_id': horizontal_result.video_id,
                }),
            )
        response = {
            'asset_id': str(asset['id']),
            'video_id': result.video_id,
            'status': 'rendering',
            'kind': 'video',
            'aspect': '9:16',
            'horizontal_heygen_paid': bool(horizontal_result),
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
            WHERE kind IN ('video','final','video_h','final_h') AND meta->>'video_id'=$1
            ORDER BY CASE kind
              WHEN 'video' THEN 0 WHEN 'video_h' THEN 0
              ELSE 1 END
            FOR UPDATE
            """,
            video_id,
        )
        if not asset:
            raise LookupError('HeyGen video is not associated with a job')
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', asset['job_id'])
        is_horizontal = asset['kind'] in ('video_h', 'final_h') or (
            isinstance(asset['meta'], dict) and asset['meta'].get('role') == 'horizontal'
        ) or (
            isinstance(asset['meta'], str) and '"horizontal"' in asset['meta']
        )
        meta = asset['meta']
        if isinstance(meta, str):
            meta = json.loads(meta)
        meta = meta or {}
        is_horizontal = is_horizontal or meta.get('role') == 'horizontal' or meta.get('aspect') == '16:9'

        # Horizontal sibling completion: update asset only; do not drive primary status machine
        if is_horizontal and asset['kind'] in ('video_h', 'final_h'):
            if event_type.endswith('.fail') or event_type.endswith('.failed'):
                await conn.execute(
                    "UPDATE assets SET meta = meta || $2::jsonb WHERE id=$1",
                    asset['id'], json.dumps({'status': 'failed', 'provider_payload': event_data}),
                )
                return {'job_id': str(job['id']), 'status': job['status'], 'horizontal': 'failed'}
            url = event_data.get('video_url') or event_data.get('url')
            await conn.execute(
                "UPDATE assets SET meta = meta || $2::jsonb WHERE id=$1",
                asset['id'],
                json.dumps({'status': 'completed', 'provider_payload': event_data}),
            )
            final_h = await conn.fetchrow(
                """
                INSERT INTO assets(job_id,kind,url,storage_path,meta)
                VALUES($1,'final_h',$2,$3,$4::jsonb)
                ON CONFLICT (job_id, kind) DO UPDATE
                  SET url = COALESCE(EXCLUDED.url, assets.url),
                      meta = assets.meta || EXCLUDED.meta
                RETURNING *
                """,
                job['id'], url, asset['storage_path'] or f'heygen/{video_id}.mp4',
                json.dumps({
                    'provider': 'heygen', 'video_id': video_id, 'status': 'completed',
                    'aspect': '16:9', 'role': 'horizontal', 'heygen_paid': True,
                    'provider_payload': event_data,
                }),
            )
            await conn.execute(
                """
                UPDATE jobs SET meta = jsonb_set(
                  COALESCE(meta, '{}'::jsonb),
                  '{formats,horizontal,status}',
                  '"ready"'::jsonb, true
                ), updated_at=now() WHERE id=$1
                """,
                job['id'],
            )
            return {
                'job_id': str(job['id']),
                'asset_id': str(final_h['id']),
                'video_id': video_id,
                'status': job['status'],
                'horizontal': 'ready',
                'kind': 'final_h',
            }

        if job['status'] in ('rendered', 'staged', 'approved', 'delivered'):
            return {
                'job_id': str(job['id']), 'asset_id': str(asset['id']),
                'video_id': video_id, 'status': job['status'],
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
                'aspect': '9:16',
                'role': 'primary',
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
        # Phase 3 gate: stage for human approval — do NOT auto-publish / deliver
        response = await _stage_for_approval(conn, job['id'], response)
    await outbox.flush_outbox(limit=10)
    try:
        from . import metrics
        await metrics.incr('jobs_rendered', labels={'provider': 'heygen'})
        await metrics.incr('jobs_staged', labels={'provider': 'heygen'})
        await metrics.record_job_cost(
            job['id'],
            metrics.estimate_cost(heygen_paid_calls=1, used_remotion=False),
        )
    except Exception:
        pass
    return response


async def distribute(job_id: UUID) -> dict:
    """Phase 4 distribution — approval-gated.

    Advances approved → delivered ONLY after explicit approve.
    Always writes staging package. Calls YouTube when OAuth env is set and a
    local video file exists; otherwise staging-only (public_post=false).
    TikTok / Reels remain documented-only (captions staged, never auto-posted).

    Audit F3: if already delivered, or a YouTube upload id is already persisted,
    short-circuit success without starting another resumable upload.
    """
    from . import metrics
    from .distribute import run_distribution

    async with transaction() as conn:
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job:
            raise LookupError('job not found')
        # Idempotent success if already distributed
        if job['status'] == 'delivered':
            meta = _job_meta(job)
            return {
                'job_id': str(job_id),
                'status': 'delivered',
                'public_post': bool((meta.get('distribution') or {}).get('public_post')),
                'mode': (meta.get('distribution') or {}).get('mode') or 'idempotent',
                'staging_path': (meta.get('distribution') or {}).get('staging_path'),
                'results': (meta.get('distribution') or {}).get('results') or [],
                'idempotent': True,
                'stub': False,
            }
        if job['status'] != 'approved':
            raise ValueError(
                f'refuse distribute: job status is {job["status"]}, expected approved '
                f'(nothing auto-publishes without explicit approve)'
            )
        final = await conn.fetchrow(
            "SELECT * FROM assets WHERE job_id=$1 AND kind='final'",
            job_id,
        )
        job_dict = dict(job)
        final_dict = dict(final) if final else None
        # YouTube idempotency: run_distribution short-circuits on persisted upload id

    dist = await run_distribution(job_id=job_id, job_row=job_dict, final_asset=final_dict)

    # Hard fail live upload errors so work_queue can retry — but staging-only is success
    live_failures = [
        r for r in dist.get('results') or []
        if r.get('mode') == 'failed'
    ]
    # If youtube was configured and failed with no staging fallback desired for retry:
    # We still advance to delivered for staging_only; for hard live failures with
    # configured credentials, raise so worker retries.
    from ..config import settings as _settings
    if live_failures and _settings.youtube_configured:
        # Record failure metrics but do not mark delivered / public_post
        await metrics.incr('jobs_failed', labels={'step': 'distribute'})
        for r in live_failures:
            if r.get('latency_ms') is not None:
                await metrics.observe('youtube', float(r['latency_ms']), labels={'outcome': 'failed'})
        raise RuntimeError(
            'distribute live failure: ' + '; '.join(
                (r.get('error') or r.get('platform') or 'unknown') for r in live_failures
            )
        )

    public_post = bool(dist.get('public_post'))
    async with transaction() as conn:
        # Refresh captions published flags + distribution meta
        meta = _job_meta(await conn.fetchrow('SELECT meta FROM jobs WHERE id=$1', job_id))
        meta['platform_captions'] = dist.get('platform_captions') or meta.get('platform_captions') or {}
        meta['distribution'] = {
            'mode': dist.get('mode'),
            'public_post': public_post,
            'staging_path': dist.get('staging_path'),
            'results': dist.get('results'),
            'documented_only': dist.get('documented_only'),
            'status': 'delivered',
        }
        await conn.execute(
            'UPDATE jobs SET meta=$2::jsonb, updated_at=now() WHERE id=$1',
            job_id, json.dumps(meta),
        )
        await advance(conn, job_id, 'delivered', {
            'via': 'distribute',
            'mode': dist.get('mode'),
            'public_post': public_post,
            'staging_path': dist.get('staging_path'),
            'platforms': list((dist.get('platform_captions') or {}).keys()),
        })

    # Metrics
    await metrics.incr('jobs_delivered', labels={'mode': dist.get('mode') or 'unknown'})
    if public_post or dist.get('mode') == 'live':
        await metrics.incr('jobs_distributed_live')
    else:
        await metrics.incr('jobs_distributed_staging')
    for r in dist.get('results') or []:
        if r.get('latency_ms') is not None:
            await metrics.observe(
                r.get('platform') or 'provider',
                float(r['latency_ms']),
                labels={'mode': r.get('mode'), 'outcome': 'ok' if r.get('mode') == 'live' else r.get('mode')},
            )

    return {
        'job_id': str(job_id),
        'status': 'delivered',
        'public_post': public_post,
        'mode': dist.get('mode'),
        'staging_path': dist.get('staging_path'),
        'results': dist.get('results'),
        'stub': False,
    }


async def distribute_stub(job_id: UUID) -> dict:
    """Backward-compatible alias — Phase 4 real distribute (staging-safe)."""
    return await distribute(job_id)


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
