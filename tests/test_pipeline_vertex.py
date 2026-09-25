"""Vertex + ffmpeg pipeline wiring: 3-clip generation, poll-only reconcile,
ffmpeg stitch, and image-gate/idempotency parity with the HeyGen path these
tests mirror (see tests/test_pipeline_phase1.py)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.postgres


async def _apply_schema(conn):
    from tests.conftest import apply_schema
    await apply_schema(conn)


@pytest.fixture
async def pg_pool(require_database_url):
    import asyncpg
    from app import db as dbmod

    await dbmod.close()
    pool = await asyncpg.create_pool(require_database_url, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await _apply_schema(conn)
        from tests.conftest import truncate_app_tables
        await truncate_app_tables(conn)
    dbmod._pool = pool
    yield pool
    await pool.close()
    dbmod._pool = None


async def _make_job(pg_pool, *, status='script_ready', script=None):
    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, script, status) VALUES($1, $2::jsonb, $3) RETURNING *",
            'full script text', json.dumps(script or {'hook': 'Stop!', 'body': 'Do the thing.', 'cta': 'Follow now.'}),
            status,
        )
        return job['id']


@pytest.mark.asyncio
@pytest.mark.soft_image_gate
async def test_generate_vertex_clips_starts_three_clips_and_uses_kind_video_clip(pg_pool):
    from app.services import pipeline
    from app.services.vertex import VertexOperation

    job_id = await _make_job(pg_pool)

    ops = [VertexOperation(operation_name=f'op-{i}', done=False) for i in range(3)]
    with patch('app.services.pipeline.vertex.start_clip_generation', AsyncMock(side_effect=ops)):
        result = await pipeline.generate_vertex_clips(job_id)

    assert result['status'] == 'rendering'
    assert result['kind'] == 'video_clip'
    assert result['clip_count'] == 3
    assert [c['operation_name'] for c in result['clips']] == ['op-0', 'op-1', 'op-2']
    assert all(c['status'] == 'running' for c in result['clips'])

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
        asset = await conn.fetchrow("SELECT * FROM assets WHERE job_id=$1 AND kind='video_clip'", job_id)
        keys = await conn.fetch(
            "SELECT key FROM idempotency_keys WHERE job_id=$1 ORDER BY key", job_id
        )
    assert job['status'] == 'rendering'
    assert asset is not None
    key_names = {k['key'] for k in keys}
    assert f'{job_id}:vertex-clips' in key_names
    for i in range(3):
        assert f'{job_id}:vertex-clip-{i}' in key_names


@pytest.mark.asyncio
async def test_generate_vertex_clips_refuses_without_image_pass(pg_pool):
    """Unmarked path must hit the real fail-closed gate, same as HeyGen (#19)."""
    from app.services import pipeline
    from app.services.image_gate import GateRefuse

    job_id = await _make_job(pg_pool)

    with pytest.raises(GateRefuse):
        await pipeline.generate_vertex_clips(job_id)


@pytest.mark.asyncio
@pytest.mark.soft_image_gate
async def test_generate_vertex_clips_uses_hook_body_cta_as_prompts(pg_pool):
    from app.services import pipeline
    from app.services.vertex import VertexOperation

    job_id = await _make_job(
        pg_pool, script={'hook': 'HOOK TEXT', 'body': 'BODY TEXT', 'cta': 'CTA TEXT'}
    )

    captured_prompts = []

    async def _fake_start(*, job_id, prompt, clip_index, reference_images=None):
        captured_prompts.append(prompt)
        return VertexOperation(operation_name=f'op-{clip_index}', done=False)

    with patch('app.services.pipeline.vertex.start_clip_generation', AsyncMock(side_effect=_fake_start)):
        await pipeline.generate_vertex_clips(job_id)

    assert captured_prompts == ['HOOK TEXT', 'BODY TEXT', 'CTA TEXT']


@pytest.mark.asyncio
@pytest.mark.soft_image_gate
async def test_generate_vertex_clips_is_idempotent_on_replay(pg_pool):
    from app.services import pipeline
    from app.services.vertex import VertexOperation

    job_id = await _make_job(pg_pool)
    ops = [VertexOperation(operation_name=f'op-{i}', done=False) for i in range(3)]
    with patch('app.services.pipeline.vertex.start_clip_generation', AsyncMock(side_effect=ops)) as mocked:
        first = await pipeline.generate_vertex_clips(job_id)
        second = await pipeline.generate_vertex_clips(job_id)

    assert first == second
    assert mocked.await_count == 3  # not called again on replay


async def _seed_rendering_job_with_clips(pg_pool, *, clips):
    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('script', 'rendering') RETURNING *"
        )
        job_id = job['id']
        await conn.execute(
            "INSERT INTO assets(job_id, kind, meta) VALUES($1, 'video_clip', $2::jsonb)",
            job_id, json.dumps({'provider': 'vertex', 'clips': clips}),
        )
        for i in range(len(clips)):
            await conn.execute(
                "INSERT INTO idempotency_keys(key, job_id, step, result) VALUES($1,$2,$3,$4::jsonb)",
                f'{job_id}:vertex-clip-{i}', job_id, f'vertex-clip-{i}', json.dumps(clips[i]),
            )
        await conn.execute(
            "INSERT INTO idempotency_keys(key, job_id, step, result) VALUES($1,$2,$3,$4::jsonb)",
            f'{job_id}:vertex-clips', job_id, 'vertex-clips',
            json.dumps({'status': 'rendering', 'kind': 'video_clip'}),
        )
    return job_id


@pytest.mark.asyncio
async def test_reconcile_vertex_clips_stays_pending_until_all_three_done(pg_pool):
    from app.services import pipeline
    from app.services.vertex import VertexOperation

    clips = [
        {'clip_index': i, 'operation_name': f'op-{i}', 'status': 'running', 'prompt': 'p'}
        for i in range(3)
    ]
    job_id = await _seed_rendering_job_with_clips(pg_pool, clips=clips)

    async def _fake_get(operation_name):
        if operation_name == 'op-0':
            return VertexOperation(operation_name='op-0', done=True, video_bytes_b64='ZmFrZQ==', mime_type='video/mp4')
        return VertexOperation(operation_name=operation_name, done=False)

    with patch('app.services.pipeline.vertex.get_clip_operation', AsyncMock(side_effect=_fake_get)), \
         patch('app.services.pipeline.storage.save_bytes', lambda rel, data: rel), \
         patch('app.services.pipeline.storage.absolute_path', lambda rel: f'/tmp/{rel}'):
        result = await pipeline.reconcile_vertex_clips(job_id)

    assert result['reconciled'] is False
    assert result['pending'] == [1, 2]

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
        asset = await conn.fetchrow("SELECT meta FROM assets WHERE job_id=$1 AND kind='video_clip'", job_id)
    assert job['status'] == 'rendering'
    meta = asset['meta'] if not isinstance(asset['meta'], str) else json.loads(asset['meta'])
    clip0 = next(c for c in meta['clips'] if c['clip_index'] == 0)
    assert clip0['status'] == 'completed'
    assert clip0['local_path'] == '/tmp/vertex/{}-clip-0.mp4'.format(job_id)


@pytest.mark.asyncio
async def test_reconcile_vertex_clips_stages_for_approval_once_all_done(pg_pool):
    from app.services import pipeline
    from app.services.vertex import VertexOperation

    clips = [
        {'clip_index': i, 'operation_name': f'op-{i}', 'status': 'running', 'prompt': 'p'}
        for i in range(3)
    ]
    job_id = await _seed_rendering_job_with_clips(pg_pool, clips=clips)

    async def _fake_get(operation_name):
        return VertexOperation(operation_name=operation_name, done=True, video_bytes_b64='ZmFrZQ==', mime_type='video/mp4')

    fake_render_result = {
        'url': None, 'storage_path': f'final/{job_id}.mp4',
        'meta': {'provider': 'ffmpeg', 'clip_count': 3, 'caption_count': 0},
    }

    with patch('app.services.pipeline.vertex.get_clip_operation', AsyncMock(side_effect=_fake_get)), \
         patch('app.services.pipeline.storage.save_bytes', lambda rel, data: rel), \
         patch('app.services.pipeline.storage.absolute_path', lambda rel: f'/tmp/{rel}'), \
         patch('app.services.pipeline.ffmpeg_render.render', AsyncMock(return_value=fake_render_result)):
        result = await pipeline.reconcile_vertex_clips(job_id)

    assert result['status'] == 'staged'
    assert result['kind'] == 'final'
    assert result['awaiting_approval'] is True

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
        final = await conn.fetchrow("SELECT * FROM assets WHERE job_id=$1 AND kind='final'", job_id)
    assert job['status'] == 'staged'
    assert final is not None
    assert final['storage_path'] == f'final/{job_id}.mp4'


@pytest.mark.asyncio
async def test_reconcile_vertex_clips_fails_job_when_a_clip_errors(pg_pool):
    from app.services import pipeline
    from app.services.vertex import VertexOperation

    clips = [
        {'clip_index': i, 'operation_name': f'op-{i}', 'status': 'running', 'prompt': 'p'}
        for i in range(3)
    ]
    job_id = await _seed_rendering_job_with_clips(pg_pool, clips=clips)

    async def _fake_get(operation_name):
        if operation_name == 'op-1':
            return VertexOperation(operation_name='op-1', done=True, error='safety filter blocked generation')
        return VertexOperation(operation_name=operation_name, done=True, video_bytes_b64='ZmFrZQ==')

    with patch('app.services.pipeline.vertex.get_clip_operation', AsyncMock(side_effect=_fake_get)), \
         patch('app.services.pipeline.storage.save_bytes', lambda rel, data: rel), \
         patch('app.services.pipeline.storage.absolute_path', lambda rel: f'/tmp/{rel}'):
        result = await pipeline.reconcile_vertex_clips(job_id)

    assert result['status'] == 'failed'
    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
        key = await conn.fetchrow(
            'SELECT * FROM idempotency_keys WHERE key=$1', f'{job_id}:vertex-clips'
        )
    assert job['status'] == 'failed'
    assert key is None  # cleared for retry, same convention as HeyGen's _fail_step


@pytest.mark.asyncio
async def test_reconcile_job_routes_to_vertex_when_video_clip_asset_present(pg_pool):
    from app.services import pipeline

    clips = [{'clip_index': 0, 'operation_name': 'op-0', 'status': 'running', 'prompt': 'p'}]
    job_id = await _seed_rendering_job_with_clips(pg_pool, clips=clips)

    with patch(
        'app.services.pipeline.reconcile_vertex_clips',
        AsyncMock(return_value={'job_id': str(job_id), 'status': 'rendering', 'reconciled': False}),
    ) as routed:
        result = await pipeline.reconcile_job(job_id)

    routed.assert_awaited_once_with(job_id)
    assert result['reconciled'] is False


@pytest.mark.asyncio
async def test_reconcile_job_still_uses_heygen_path_when_no_video_clip_asset(pg_pool):
    """Existing HeyGen reconcile behavior is unchanged by the new routing."""
    from app.services import pipeline
    from app.services.heygen import HeyGenVideo

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('script', 'rendering') RETURNING *"
        )
        job_id = job['id']
        await conn.execute(
            "INSERT INTO assets(job_id, kind, meta) VALUES($1, 'video', $2::jsonb)",
            job_id, json.dumps({'provider': 'heygen', 'video_id': 'vid_unchanged'}),
        )

    fake = HeyGenVideo(video_id='vid_unchanged', status='failed', raw={'status': 'failed'})
    with patch('app.services.pipeline.heygen.get_video', AsyncMock(return_value=fake)):
        result = await pipeline.reconcile_job(job_id)

    assert result['status'] == 'failed'
