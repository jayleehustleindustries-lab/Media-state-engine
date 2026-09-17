"""Pipeline Phase 1: asset kinds, fail-on-provider-error, reconcile (mocked HeyGen)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.postgres


async def _apply_schema(conn):
    schema = Path(__file__).resolve().parents[1] / 'schema.sql'
    await conn.execute(schema.read_text())


@pytest.fixture
async def pg_pool(require_database_url):
    import asyncpg
    from app import db as dbmod

    await dbmod.close()
    pool = await asyncpg.create_pool(require_database_url, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await _apply_schema(conn)
        await conn.execute('TRUNCATE webhook_outbox, idempotency_keys, events, assets, jobs CASCADE')
    dbmod._pool = pool
    yield pool
    await pool.close()
    dbmod._pool = None


@pytest.mark.asyncio
async def test_generate_avatar_uses_kind_video(pg_pool, monkeypatch):
    from app.services import pipeline
    from app.services.heygen import HeyGenVideo

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('script', 'script_ready') RETURNING *"
        )
        job_id = job['id']

    fake = HeyGenVideo(video_id='vid_1', status='processing')
    with patch('app.services.pipeline.heygen.create_video', AsyncMock(return_value=fake)):
        result = await pipeline.generate_avatar(job_id)

    assert result['kind'] == 'video'
    assert result['status'] == 'rendering'

    async with pg_pool.acquire() as conn:
        video = await conn.fetchrow("SELECT * FROM assets WHERE job_id=$1 AND kind='video'", job_id)
        final = await conn.fetchrow("SELECT * FROM assets WHERE job_id=$1 AND kind='final'", job_id)
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
    assert video is not None
    assert final is None
    assert job['status'] == 'rendering'


@pytest.mark.asyncio
async def test_heygen_success_promotes_final(pg_pool):
    from app.services import pipeline

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('script', 'rendering') RETURNING *"
        )
        job_id = job['id']
        await conn.execute(
            """
            INSERT INTO assets(job_id, kind, storage_path, meta)
            VALUES($1, 'video', 'heygen/vid_2.mp4', $2::jsonb)
            """,
            job_id,
            json.dumps({'provider': 'heygen', 'video_id': 'vid_2', 'status': 'processing'}),
        )

    result = await pipeline.complete_heygen_webhook(
        'avatar_video.success',
        'vid_2',
        {'video_id': 'vid_2', 'video_url': 'https://cdn/final.mp4'},
    )
    assert result['status'] == 'rendered'
    assert result['kind'] == 'final'

    async with pg_pool.acquire() as conn:
        final = await conn.fetchrow("SELECT * FROM assets WHERE job_id=$1 AND kind='final'", job_id)
        video = await conn.fetchrow("SELECT * FROM assets WHERE job_id=$1 AND kind='video'", job_id)
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
    assert final['url'] == 'https://cdn/final.mp4'
    assert video is not None
    assert job['status'] == 'rendered'


@pytest.mark.asyncio
async def test_provider_error_fails_and_clears_key(pg_pool):
    from app.services import pipeline
    from app.services.heygen import HeyGenRequestError

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('script', 'script_ready') RETURNING *"
        )
        job_id = job['id']

    with patch(
        'app.services.pipeline.heygen.create_video',
        AsyncMock(side_effect=HeyGenRequestError('provider down', status_code=500)),
    ):
        with pytest.raises(HeyGenRequestError):
            await pipeline.generate_avatar(job_id)

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
        key = await conn.fetchrow(
            'SELECT * FROM idempotency_keys WHERE key=$1', f'{job_id}:heygen-avatar'
        )
        events = await conn.fetch(
            "SELECT to_status FROM events WHERE job_id=$1 ORDER BY id", job_id
        )
    assert job['status'] == 'failed'
    assert key is None  # cleared for retry
    assert any(e['to_status'] == 'failed' for e in events)


@pytest.mark.asyncio
async def test_reconcile_applies_provider_failure(pg_pool):
    from app.services import pipeline
    from app.services.heygen import HeyGenVideo

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('script', 'rendering') RETURNING *"
        )
        job_id = job['id']
        await conn.execute(
            """
            INSERT INTO assets(job_id, kind, meta)
            VALUES($1, 'video', $2::jsonb)
            """,
            job_id,
            json.dumps({'provider': 'heygen', 'video_id': 'vid_fail'}),
        )

    fake = HeyGenVideo(video_id='vid_fail', status='failed', raw={'status': 'failed'})
    with patch('app.services.pipeline.heygen.get_video', AsyncMock(return_value=fake)):
        result = await pipeline.reconcile_job(job_id)

    assert result['status'] == 'failed'
    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
    assert job['status'] == 'failed'
