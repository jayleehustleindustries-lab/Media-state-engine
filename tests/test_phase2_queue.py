"""Phase 2: Postgres work queue, async enqueue API, job detail, worker tick."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

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


@pytest.mark.asyncio
async def test_enqueue_step_dedupes_active_work(pg_pool):
    from app.services import queue

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('hello', 'script_ready') RETURNING *"
        )
        job_id = job["id"]

    first = await queue.enqueue_step(job_id, "generate_audio")
    second = await queue.enqueue_step(job_id, "generate_audio")
    assert first["id"] == second["id"]
    assert first["status"] == "pending"

    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM work_queue WHERE job_id=$1 AND step='generate_audio'",
            job_id,
        )
    assert count == 1


@pytest.mark.asyncio
async def test_claim_and_mark_done(pg_pool):
    from app.db import transaction
    from app.services import queue

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('hello', 'script_ready') RETURNING *"
        )
        job_id = job["id"]

    await queue.enqueue_step(job_id, "generate_avatar", {"avatar_id": "a1"})

    async with transaction() as conn:
        claimed = await queue.claim_due(conn, limit=5)
        assert len(claimed) == 1
        assert claimed[0]["step"] == "generate_avatar"
        assert claimed[0]["status"] == "running"
        assert claimed[0]["attempts"] == 1
        await queue.mark_done(conn, claimed[0]["id"], {"ok": True})

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM work_queue WHERE job_id=$1", job_id)
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_mark_failure_retries_then_dead(pg_pool):
    from app.db import transaction
    from app.services import queue

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('hello', 'script_ready') RETURNING *"
        )
        job_id = job["id"]

    work = await queue.enqueue_step(job_id, "render")
    work_id = int(work["id"])

    for attempt in range(1, 4):
        async with transaction() as conn:
            await conn.execute(
                "UPDATE work_queue SET status='running', attempts=$2 WHERE id=$1",
                work_id,
                attempt,
            )
            status = await queue.mark_failure(conn, work_id, "boom", attempt, 3)
        if attempt < 3:
            assert status == "pending"
        else:
            assert status == "dead"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM work_queue WHERE id=$1", work_id)
    assert row["status"] == "dead"
    assert row["attempts"] >= 3


@pytest.mark.asyncio
async def test_worker_tick_runs_generate_avatar(pg_pool):
    from app.services.heygen import HeyGenVideo
    from app import worker
    from app.services import queue

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('script', 'script_ready') RETURNING *"
        )
        job_id = job["id"]

    await queue.enqueue_step(job_id, "generate_avatar")

    fake = HeyGenVideo(video_id="vid_p2", status="processing")
    with patch("app.services.pipeline.heygen.create_video", AsyncMock(return_value=fake)):
        stats = await worker.tick(also_reconcile=False)

    assert stats["claimed"] == 1
    assert stats["processed"] == 1

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow("SELECT status FROM jobs WHERE id=$1", job_id)
        video = await conn.fetchrow(
            "SELECT * FROM assets WHERE job_id=$1 AND kind='video'", job_id
        )
        work = await conn.fetchrow("SELECT status FROM work_queue WHERE job_id=$1", job_id)
    assert job["status"] == "rendering"
    assert video is not None
    assert work["status"] == "done"


@pytest.mark.asyncio
async def test_job_detail_includes_assets_events_work(pg_pool):
    from app.services import jobs, queue
    from app.state_machine import advance
    from app.db import transaction

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('script', 'pending') RETURNING *"
        )
        job_id = job["id"]

    async with transaction() as conn:
        await advance(conn, job_id, "script_ready", {"via": "test"})
    await queue.enqueue_step(job_id, "generate_audio")

    detail = await jobs.get_job_detail(job_id)
    assert detail is not None
    assert detail["status"] == "script_ready"
    assert len(detail["status_history"]) == 1
    assert detail["status_history"][0]["to_status"] == "script_ready"
    assert len(detail["work"]) == 1
    assert detail["work"][0]["step"] == "generate_audio"
    assert detail["assets"] == []


@pytest.mark.asyncio
async def test_api_generate_audio_returns_202(pg_pool):
    """Route returns 202 with work payload; enqueue is mocked to avoid TestClient/pool fights."""
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock, patch
    import os
    from uuid import uuid4

    key = os.environ["MEDIA_ENGINE_API_KEY"]
    job_id = uuid4()
    fake_work = {
        "id": 42,
        "job_id": job_id,
        "step": "generate_audio",
        "status": "pending",
        "job_status": "script_ready",
        "attempts": 0,
    }

    with patch("app.main.connect", new_callable=AsyncMock), patch(
        "app.main.close", new_callable=AsyncMock
    ), patch(
        "app.api.jobs.queue.enqueue_step", new_callable=AsyncMock, return_value=fake_work
    ):
        from app.main import app

        with TestClient(app) as client:
            r = client.post(
                f"/jobs/{job_id}/generate-audio",
                headers={"Authorization": f"Bearer {key}"},
            )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queued"] is True
    assert body["step"] == "generate_audio"
    assert body["work_id"] == 42



@pytest.mark.asyncio
async def test_outbox_success_does_not_auto_deliver(pg_pool, monkeypatch):
    """Phase 3: outbox success must NOT advance job to delivered (approval gate)."""
    from app.services import outbox
    from app.db import transaction
    from app.config import settings

    # Even if misconfigured True, Phase 3 hard-blocks auto-deliver
    monkeypatch.setattr(settings, "auto_deliver_on_outbox_success", True)
    monkeypatch.setattr(settings, "webhook_url", "http://example.test/hook")
    monkeypatch.setattr(settings, "webhook_secret", "sekrit")

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow(
            "INSERT INTO jobs(script_text, status) VALUES('script', 'staged') RETURNING *"
        )
        job_id = job["id"]

    async with transaction() as conn:
        await outbox.enqueue(conn, job_id, {"status": "staged", "job_id": str(job_id)})

    async def ok_deliver(url, payload):
        assert url == "http://example.test/hook"
        assert payload["status"] == "staged"

    async with transaction() as conn:
        stats = await outbox.process_due(conn, deliver=ok_deliver)

    assert stats["delivered"] == 1
    assert stats["jobs_marked_delivered"] == 0
    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow("SELECT status FROM jobs WHERE id=$1", job_id)
    assert job["status"] == "staged"


@pytest.mark.asyncio
async def test_heygen_create_video_includes_callback_url(monkeypatch):
    from app.services import heygen
    from app.config import settings
    from unittest.mock import patch
    from uuid import uuid4

    monkeypatch.setattr(settings, "heygen_api_key", "test-key")
    monkeypatch.setattr(settings, "heygen_avatar_id", "av1")
    monkeypatch.setattr(settings, "heygen_voice_id", "v1")
    monkeypatch.setattr(settings, "public_base_url", "https://engine.example.com")

    captured = {}

    class FakeResp:
        is_success = True
        status_code = 200

        def json(self):
            return {"data": {"video_id": "vid_cb", "status": "processing"}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResp()

    with patch("app.services.heygen.httpx.AsyncClient", FakeClient):
        result = await heygen.create_video(
            job_id=uuid4(),
            script_text="hello world",
            idempotency_key="job:test:heygen-avatar",
        )
    assert result.video_id == "vid_cb"
    assert captured["json"]["callback_url"] == "https://engine.example.com/webhooks/heygen"
    assert captured["headers"].get("Idempotency-Key") == "job:test:heygen-avatar"
