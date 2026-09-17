"""Phase 4: scheduler cadence, distribute gate, YouTube stub, metrics."""
from __future__ import annotations

import json
import os
from pathlib import Path
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
    pool = await asyncpg.create_pool(require_database_url, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await apply_schema(conn)
        from tests.conftest import truncate_app_tables
        await truncate_app_tables(conn)
    dbmod._pool = pool
    yield pool
    await pool.close()
    dbmod._pool = None


def test_youtube_distributor_not_configured():
    from app.services.distribute.youtube import YouTubeDistributor
    from app.config import settings

    d = YouTubeDistributor()
    # Default test env has empty youtube creds
    assert settings.youtube_configured is False
    assert d.configured() is False


@pytest.mark.asyncio
async def test_youtube_publish_staging_when_no_creds():
    from app.services.distribute.youtube import YouTubeDistributor

    d = YouTubeDistributor()
    result = await d.publish(
        job_id="abc",
        title="t",
        description="d",
        tags=["shorts"],
        video_path=None,
        video_url="https://example.com/v.mp4",
        caption_meta={},
    )
    assert result.mode == "staging_only"
    assert result.public_post is False


@pytest.mark.asyncio
async def test_scheduler_tick_respects_daily_cap(pg_pool, monkeypatch):
    from app.config import settings
    from app.services import scheduler

    monkeypatch.setattr(settings, "schedule_posts_per_day", 2)
    monkeypatch.setattr(settings, "schedule_enqueue_avatar", False)

    r1 = await scheduler.tick(force=False)
    assert r1["created_count"] == 2
    assert r1["skipped"] is False

    r2 = await scheduler.tick()
    assert r2["created_count"] == 0
    assert r2["skipped"] is True
    assert "cadence" in (r2.get("reason") or "")

    st = await scheduler.status()
    assert st["created_today"] == 2
    assert st["remaining"] == 0


@pytest.mark.asyncio
async def test_scheduler_force_creates_more(pg_pool, monkeypatch):
    from app.config import settings
    from app.services import scheduler

    monkeypatch.setattr(settings, "schedule_posts_per_day", 1)
    await scheduler.tick()
    forced = await scheduler.tick(count=1, force=True, topics=["forced topic"])
    assert forced["created_count"] == 1
    assert forced["created"][0]["topic"] == "forced topic"


@pytest.mark.asyncio
async def test_distribute_refuses_without_approve(pg_pool):
    from app.services import jobs, pipeline

    row = await jobs.create_job(topic="gate check")
    with pytest.raises(ValueError, match="approve"):
        await pipeline.distribute(row["id"])


@pytest.mark.asyncio
async def test_distribute_staging_only_marks_captions_unpublished(pg_pool, tmp_path, monkeypatch):
    from app.config import settings
    from app.services import jobs, pipeline
    from app.state_machine import advance
    from app.db import transaction

    monkeypatch.setattr(settings, "distribute_staging_dir", str(tmp_path / "staging"))
    # Ensure youtube not configured
    monkeypatch.setattr(settings, "youtube_client_id", "")
    monkeypatch.setattr(settings, "youtube_client_secret", "")
    monkeypatch.setattr(settings, "youtube_refresh_token", "")

    row = await jobs.create_job(topic="staging distribute")
    job_id = row["id"]
    async with transaction() as conn:
        await conn.execute("UPDATE jobs SET status='rendering' WHERE id=$1", job_id)
        await advance(conn, job_id, "rendered", {"via": "test"})
        await advance(conn, job_id, "staged", {"via": "test"})
        await conn.execute(
            """
            INSERT INTO assets(job_id, kind, url, storage_path, meta)
            VALUES($1, 'final', 'https://example.com/v.mp4', NULL, '{}'::jsonb)
            """,
            job_id,
        )

    await jobs.approve_job(job_id, approved_by="jordan", enqueue_distribute=False)
    dist = await pipeline.distribute(job_id)
    assert dist["status"] == "delivered"
    assert dist["public_post"] is False
    assert dist["mode"] == "staging_only"
    assert dist["staging_path"]
    assert Path(dist["staging_path"]).joinpath("manifest.json").exists()

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow("SELECT status, meta FROM jobs WHERE id=$1", job_id)
    assert job["status"] == "delivered"
    meta = job["meta"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    caps = meta["platform_captions"]
    assert caps["tiktok"]["published"] is False
    assert caps["reels"]["published"] is False
    assert caps["shorts"]["published"] is False
    assert meta["distribution"]["public_post"] is False


@pytest.mark.asyncio
async def test_distribute_live_youtube_marks_shorts_published(pg_pool, tmp_path, monkeypatch):
    from app.config import settings
    from app.services import jobs, pipeline
    from app.services.distribute.base import DistributeResult
    from app.state_machine import advance
    from app.db import transaction

    monkeypatch.setattr(settings, "distribute_staging_dir", str(tmp_path / "staging"))
    monkeypatch.setattr(settings, "youtube_client_id", "cid")
    monkeypatch.setattr(settings, "youtube_client_secret", "sec")
    monkeypatch.setattr(settings, "youtube_refresh_token", "rt")
    monkeypatch.setattr(settings, "youtube_privacy_status", "public")

    # Local video file required for live path
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"fake-mp4-bytes")

    row = await jobs.create_job(topic="live youtube")
    job_id = row["id"]
    async with transaction() as conn:
        await conn.execute("UPDATE jobs SET status='rendering' WHERE id=$1", job_id)
        await advance(conn, job_id, "rendered", {"via": "test"})
        await advance(conn, job_id, "staged", {"via": "test"})
        await conn.execute(
            """
            INSERT INTO assets(job_id, kind, url, storage_path, meta)
            VALUES($1, 'final', NULL, $2, '{}'::jsonb)
            """,
            job_id,
            str(vid),
        )

    await jobs.approve_job(job_id, approved_by="jordan", enqueue_distribute=False)

    async def fake_publish(**kwargs):
        return DistributeResult(
            platform="youtube",
            mode="live",
            public_post=True,
            external_id="yt_abc123",
            latency_ms=12.5,
        )

    with patch("app.services.distribute.YouTubeDistributor.publish", new=fake_publish):
        # patch instance method used via get_distributors — patch module-level instance
        with patch("app.services.distribute._YOUTUBE.publish", new=fake_publish):
            dist = await pipeline.distribute(job_id)

    assert dist["public_post"] is True
    assert dist["mode"] == "live"
    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow("SELECT meta FROM jobs WHERE id=$1", job_id)
    meta = job["meta"] if not isinstance(job["meta"], str) else json.loads(job["meta"])
    assert meta["platform_captions"]["shorts"]["published"] is True
    assert meta["platform_captions"]["tiktok"]["published"] is False


@pytest.mark.asyncio
async def test_metrics_endpoint_and_counters(pg_pool):
    from app.services import jobs, metrics, scheduler
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock, patch

    await jobs.create_job(topic="metrics job")
    await scheduler.tick(count=1, force=True)
    snap = await metrics.snapshot()
    assert "counters" in snap
    assert "jobs_by_status" in snap
    assert snap["counters"].get("jobs_created", {}).get("value", 0) >= 1

    key = os.environ["MEDIA_ENGINE_API_KEY"]
    with patch("app.main.connect", new_callable=AsyncMock), patch("app.main.close", new_callable=AsyncMock):
        from app.main import app
        with patch("app.services.metrics.snapshot", new_callable=AsyncMock) as mock_snap:
            mock_snap.return_value = {"counters": {"jobs_created": {"value": 3}}, "jobs_by_status": {}}
            with TestClient(app) as client:
                r = client.get("/admin/metrics", headers={"Authorization": f"Bearer {key}"})
                r2 = client.get("/metrics", headers={"Authorization": f"Bearer {key}"})
                unauth = client.get("/metrics")
    assert r.status_code == 200
    assert r2.status_code == 200
    assert unauth.status_code == 401
    assert r.json()["counters"]["jobs_created"]["value"] == 3


@pytest.mark.asyncio
async def test_scheduler_admin_tick_auth():
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock, patch

    key = os.environ["MEDIA_ENGINE_API_KEY"]
    with patch("app.main.connect", new_callable=AsyncMock), patch("app.main.close", new_callable=AsyncMock):
        from app.main import app
        with patch("app.services.scheduler.tick", new_callable=AsyncMock) as mock_tick:
            mock_tick.return_value = {"created_count": 0, "skipped": True}
            with TestClient(app) as client:
                bad = client.post("/admin/scheduler/tick", json={})
                ok = client.post(
                    "/admin/scheduler/tick",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"count": 1, "force": True},
                )
    assert bad.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["skipped"] is True


def test_cli_schedule_help():
    from app import cli as cli_mod
    with pytest.raises(SystemExit):
        cli_mod.main(["schedule-tick", "--help"])
