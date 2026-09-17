"""Phase 5: audit F5/F4/F3/F2 regressions."""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
async def pg_pool(require_database_url):
    import asyncpg
    from app import db as dbmod
    from tests.conftest import apply_schema

    await dbmod.close()
    pool = await asyncpg.create_pool(require_database_url, min_size=1, max_size=8)
    async with pool.acquire() as conn:
        await apply_schema(conn)
        from tests.conftest import truncate_app_tables
        await truncate_app_tables(conn)
    dbmod._pool = pool
    yield pool
    await pool.close()
    dbmod._pool = None


@pytest.mark.asyncio
async def test_f5_advance_rejects_staged_to_approved(pg_pool):
    """Generic advance must not approve — require /approve (audit F5)."""
    from app.services import jobs
    from app.state_machine import advance
    from app.db import transaction

    row = await jobs.create_job(topic="f5 gate")
    job_id = row["id"]
    async with transaction() as conn:
        await conn.execute("UPDATE jobs SET status='rendering' WHERE id=$1", job_id)
        await advance(conn, job_id, "rendered", {"via": "test"})
        await advance(conn, job_id, "staged", {"via": "test"})

    with pytest.raises(ValueError, match="approve"):
        await jobs.advance_job(job_id, "approved", {})

    with pytest.raises(ValueError, match="distribute"):
        await jobs.advance_job(job_id, "delivered", {})

    async with pg_pool.acquire() as conn:
        st = await conn.fetchval("SELECT status FROM jobs WHERE id=$1", job_id)
    assert st == "staged"


def test_f5_advance_endpoint_returns_409():
    from fastapi.testclient import TestClient

    key = os.environ["MEDIA_ENGINE_API_KEY"]
    job_id = uuid4()
    with patch("app.main.connect", new_callable=AsyncMock), patch("app.main.close", new_callable=AsyncMock):
        from app.main import app
        with patch(
            "app.api.jobs.jobs.advance_job",
            new_callable=AsyncMock,
            side_effect=ValueError(
                "cannot advance to approved via generic advance: use POST /jobs/{id}/approve"
            ),
        ):
            with TestClient(app) as client:
                r = client.post(
                    f"/jobs/{job_id}/advance",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"to_status": "approved", "payload": {}},
                )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_f4_reclaim_stale_work_and_outbox(pg_pool):
    from app.services import queue, outbox
    from app.db import transaction

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('f4', 'script_ready') RETURNING id"
        )
        job_id = job["id"]

    async with transaction() as conn:
        work = await queue.enqueue(conn, job_id, "generate_audio", {})
        work_id = int(work["id"])
        await conn.execute(
            """
            UPDATE work_queue
            SET status = 'running', started_at = now() - interval '2 hours', updated_at = now() - interval '2 hours'
            WHERE id = $1
            """,
            work_id,
        )
        box = await outbox.enqueue(conn, job_id, {"event": "test"}, destination_url="http://example.test/hook")
        box_id = int(box["id"])
        await conn.execute(
            """
            UPDATE webhook_outbox
            SET status = 'delivering', updated_at = now() - interval '2 hours'
            WHERE id = $1
            """,
            box_id,
        )

    async with transaction() as conn:
        rw = await queue.reclaim_stale_running(conn, older_than_seconds=60)
        ro = await outbox.reclaim_stale_delivering(conn, older_than_seconds=60)

    assert any(int(r["id"]) == work_id for r in rw)
    assert any(int(r["id"]) == box_id for r in ro)

    async with pg_pool.acquire() as conn:
        wstatus = await conn.fetchval("SELECT status FROM work_queue WHERE id=$1", work_id)
        ostatus = await conn.fetchval("SELECT status FROM webhook_outbox WHERE id=$1", box_id)
    assert wstatus == "pending"
    assert ostatus == "pending"


@pytest.mark.asyncio
async def test_f3_youtube_idempotent_skips_second_upload(pg_pool, tmp_path, monkeypatch):
    from app.config import settings
    from app.services import jobs, pipeline
    from app.services.distribute.base import DistributeResult
    from app.state_machine import advance
    from app.db import transaction

    monkeypatch.setattr(settings, "distribute_staging_dir", str(tmp_path / "staging"))
    monkeypatch.setattr(settings, "youtube_client_id", "cid")
    monkeypatch.setattr(settings, "youtube_client_secret", "sec")
    monkeypatch.setattr(settings, "youtube_refresh_token", "rt")
    monkeypatch.setattr(settings, "youtube_privacy_status", "private")

    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"fake-mp4")

    row = await jobs.create_job(topic="f3 idempotent")
    job_id = row["id"]
    async with transaction() as conn:
        await conn.execute("UPDATE jobs SET status='rendering' WHERE id=$1", job_id)
        await advance(conn, job_id, "rendered", {})
        await advance(conn, job_id, "staged", {})
        await conn.execute(
            "INSERT INTO assets(job_id, kind, url, storage_path, meta) VALUES($1,'final',NULL,$2,'{}'::jsonb)",
            job_id,
            str(vid),
        )
        # Simulate crash after upload persisted but before delivered
        await conn.execute(
            """
            UPDATE jobs SET meta = COALESCE(meta,'{}'::jsonb) || $2::jsonb WHERE id=$1
            """,
            job_id,
            json.dumps({"youtube_upload_id": "yt_already_uploaded"}),
        )

    await jobs.approve_job(job_id, approved_by="jordan", enqueue_distribute=False)

    publish_calls = {"n": 0}

    async def boom_publish(**kwargs):
        publish_calls["n"] += 1
        raise AssertionError("publish should not be called when upload id exists")

    with patch("app.services.distribute._YOUTUBE.publish", new=boom_publish):
        dist = await pipeline.distribute(job_id)

    assert publish_calls["n"] == 0
    assert dist["status"] == "delivered"
    assert dist["mode"] == "live"
    yt = next(r for r in dist["results"] if r["platform"] == "youtube")
    assert yt["external_id"] == "yt_already_uploaded"
    assert yt["detail"].get("idempotent") is True

    # Second call while delivered short-circuits
    again = await pipeline.distribute(job_id)
    assert again.get("idempotent") is True
    assert again["status"] == "delivered"


@pytest.mark.asyncio
async def test_f2_concurrent_ticks_respect_cap(pg_pool, monkeypatch):
    """Two overlapping ticks under advisory lock must not exceed daily cap."""
    import asyncio
    from app.config import settings
    from app.services import scheduler
    from app.db import transaction

    monkeypatch.setattr(settings, "schedule_posts_per_day", 3)
    monkeypatch.setattr(settings, "schedule_enqueue_avatar", False)

    results = await asyncio.gather(
        scheduler.tick(force=False),
        scheduler.tick(force=False),
    )
    total_created = sum(r["created_count"] for r in results)
    assert total_created == 3
    skipped = [r for r in results if r.get("skipped")]
    # One may skip for lock or cadence; at least one must create
    assert any(r["created_count"] > 0 for r in results)

    async with transaction() as conn:
        n = await conn.fetchval("SELECT count(*)::int FROM schedule_runs")
        jobs_n = await conn.fetchval("SELECT count(*)::int FROM jobs")
    assert n == 3
    # No orphan jobs beyond schedule_runs (job_id may be set on all reserved rows)
    assert jobs_n == 3
