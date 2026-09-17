"""Real Postgres concurrency tests. Skips clearly when DATABASE_URL is unset."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
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

    # Isolate from any prior app pool
    await dbmod.close()
    pool = await asyncpg.create_pool(require_database_url, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await _apply_schema(conn)
        # Clean tables for isolation
        await conn.execute('TRUNCATE work_queue, webhook_outbox, idempotency_keys, events, assets, jobs CASCADE')
    dbmod._pool = pool
    yield pool
    await pool.close()
    dbmod._pool = None


@pytest.mark.asyncio
async def test_concurrent_advance_one_winner(pg_pool):
    from app.state_machine import advance, IllegalTransition

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('x', 'pending') RETURNING id"
        )
        job_id = job['id']

    barrier = asyncio.Barrier(2)
    results: list[bool] = []

    async def attempt():
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await barrier.wait()
                try:
                    await advance(conn, job_id, 'script_ready', {})
                    results.append(True)
                except IllegalTransition:
                    results.append(False)

    await asyncio.gather(attempt(), attempt())
    assert sorted(results) == [False, True]

    async with pg_pool.acquire() as conn:
        events = await conn.fetch('SELECT * FROM events WHERE job_id=$1', job_id)
        job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
    assert len(events) == 1
    assert job['status'] == 'script_ready'


@pytest.mark.asyncio
async def test_idempotency_reserve_serializes(pg_pool):
    from app.services import jobs

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('x', 'script_ready') RETURNING id"
        )
        job_id = job['id']

    key = f'{job_id}:audio'
    barrier = asyncio.Barrier(2)
    winners = []

    async def attempt():
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await barrier.wait()
                row = await jobs.reserve_key(conn, key, job_id, 'audio')
                winners.append(row is not None)

    await asyncio.gather(attempt(), attempt())
    assert sorted(winners) == [False, True]


@pytest.mark.asyncio
async def test_expired_idempotency_key_cleared(pg_pool):
    from app.services import jobs

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('x', 'script_ready') RETURNING id"
        )
        job_id = job['id']
        key = f'{job_id}:audio'
        await conn.execute(
            """
            INSERT INTO idempotency_keys(key, job_id, step, result, expires_at)
            VALUES($1, $2, 'audio', NULL, now() - interval '1 second')
            """,
            key, job_id,
        )
        async with conn.transaction():
            got = await jobs.get_key(conn, key)
        assert got is None
        remaining = await conn.fetchrow('SELECT * FROM idempotency_keys WHERE key=$1', key)
        assert remaining is None


@pytest.mark.asyncio
async def test_outbox_survives_commit(pg_pool):
    from app.services import outbox

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('x', 'rendered') RETURNING id"
        )
        job_id = job['id']
        async with conn.transaction():
            row = await outbox.enqueue(
                conn, job_id, {'status': 'rendered'},
                destination_url='http://127.0.0.1:9/nope', max_attempts=2,
            )
            assert row is not None

    delivered = []

    async def boom(url, payload):
        raise RuntimeError('down')

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            stats = await outbox.process_due(conn, deliver=boom)
    assert stats['retried'] == 1 or stats['dead'] == 1

    async with pg_pool.acquire() as conn:
        # reset next_attempt so we can process again immediately
        await conn.execute("UPDATE webhook_outbox SET next_attempt_at = now(), status='pending'")
        async with conn.transaction():
            stats2 = await outbox.process_due(conn, deliver=boom)
        row = await conn.fetchrow('SELECT status, attempts FROM webhook_outbox WHERE job_id=$1', job_id)
    assert row['status'] == 'dead'
    assert row['attempts'] >= 2
