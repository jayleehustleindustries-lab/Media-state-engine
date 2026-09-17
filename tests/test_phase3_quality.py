"""Phase 3: script schema, dual-format cost guardrail, approval gate."""
from __future__ import annotations

import json
import os
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


def test_scriptgen_from_topic():
    from app.services import scriptgen

    full, script, meta = scriptgen.compose_script(topic="cold outreach that converts", duration_target_seconds=30)
    assert script["hook"]
    assert script["body"]
    assert script["cta"]
    assert script["duration_target_seconds"] == 30
    assert script["aspect_ratio"] == "9:16"
    assert script["full_text"] == full
    assert "tiktok" in meta["platform_captions"]
    assert "reels" in meta["platform_captions"]
    assert "shorts" in meta["platform_captions"]
    assert meta["platform_captions"]["tiktok"]["published"] is False
    assert meta["formats"]["primary"]["aspect"] == "9:16"
    assert meta["formats"]["horizontal"]["heygen_paid"] is False
    assert meta["auto_publish"] is False


def test_scriptgen_horizontal_derived_by_default():
    from app.services import scriptgen

    _, _, meta = scriptgen.compose_script(
        topic="tips", include_horizontal=True, heygen_dual_paid=False
    )
    h = meta["formats"]["horizontal"]
    assert h["mode"] == "derived"
    assert h["heygen_paid"] is False
    assert h["status"] == "staged"


def test_scriptgen_horizontal_paid_only_when_flagged():
    from app.services import scriptgen

    _, _, meta = scriptgen.compose_script(
        topic="tips", include_horizontal=True, heygen_dual_paid=True
    )
    h = meta["formats"]["horizontal"]
    assert h["mode"] == "heygen_paid"
    assert h["heygen_paid"] is True


def test_state_machine_blocks_staged_to_delivered():
    from app.state_machine import LEGACY_TRANSITIONS, TRANSITIONS
    # Legacy graph (Phase 1–5 dual-mode)
    assert "delivered" not in LEGACY_TRANSITIONS["staged"]
    assert "delivered" not in LEGACY_TRANSITIONS["rendered"]
    assert "approved" in LEGACY_TRANSITIONS["staged"]
    assert "delivered" in LEGACY_TRANSITIONS["approved"]
    assert "staged" in LEGACY_TRANSITIONS["rendered"]
    # Canonical SoT: review → approved → published (no skip-approve publish)
    assert "published" not in TRANSITIONS["review"]
    assert "published" in TRANSITIONS["approved"]
    assert "review" in TRANSITIONS["render_done"]


@pytest.mark.asyncio
async def test_create_job_stores_structured_script(pg_pool):
    from app.services import jobs

    row = await jobs.create_job(topic="morning routine for founders", duration_target_seconds=45)
    assert row["status"] == "script_ready"
    script = row["script"]
    if isinstance(script, str):
        script = json.loads(script)
    assert script["hook"]
    assert script["duration_target_seconds"] == 45
    meta = row["meta"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert "platform_captions" in meta
    assert meta["awaiting_approval"] is True


@pytest.mark.asyncio
async def test_approve_gate_and_distribute_stub(pg_pool):
    from app.services import jobs, pipeline
    from app.state_machine import IllegalTransition, advance
    from app.db import transaction

    row = await jobs.create_job(script_text="Hook! Body does the work. Follow now.")
    job_id = row["id"]

    # Fast-forward to staged (simulate post-render)
    async with transaction() as conn:
        await conn.execute("UPDATE jobs SET status='rendering' WHERE id=$1", job_id)
        await advance(conn, job_id, "rendered", {"via": "test"})
        await advance(conn, job_id, "staged", {"via": "test"})

    # Cannot deliver from staged
    with pytest.raises((IllegalTransition, ValueError)):
        await jobs.advance_job(job_id, "delivered", {})

    # Distribute stub refuses pre-approve
    with pytest.raises(ValueError, match="approve"):
        await pipeline.distribute_stub(job_id)

    result = await jobs.approve_job(job_id, approved_by="jordan", enqueue_distribute=True)
    assert result["status"] == "approved"
    assert result["public_post"] is False
    assert result["distribute_enqueued"] is True

    async with pg_pool.acquire() as conn:
        job = await conn.fetchrow("SELECT status, approved_by FROM jobs WHERE id=$1", job_id)
        work = await conn.fetchrow(
            "SELECT step, status, payload FROM work_queue WHERE job_id=$1 AND step='distribute'",
            job_id,
        )
    assert job["status"] == "approved"
    assert job["approved_by"] == "jordan"
    assert work is not None
    assert work["status"] == "pending"

    # Run stub
    dist = await pipeline.distribute_stub(job_id)
    assert dist["status"] == "delivered"
    assert dist["public_post"] is False
    # Phase 4: alias still works; staging-only when no YouTube OAuth
    assert dist.get("mode") in ("staging_only", "live", "failed") or dist.get("stub") is False
    assert dist.get("staging_path") or dist.get("stub") is True


def test_approve_endpoint_auth_and_shape():
    """HTTP approve route is auth-gated and returns approve_job payload (mocked DB)."""
    from fastapi.testclient import TestClient

    key = os.environ["MEDIA_ENGINE_API_KEY"]
    job_id = uuid4()
    with patch("app.main.connect", new_callable=AsyncMock), patch("app.main.close", new_callable=AsyncMock):
        from app.main import app
        with patch("app.api.jobs.jobs.approve_job", new_callable=AsyncMock) as mock_approve:
            mock_approve.return_value = {
                "status": "approved",
                "public_post": False,
                "approved_by": "api-user",
                "distribute_enqueued": False,
                "work_id": None,
                "job": {"id": str(job_id), "status": "approved"},
                "note": "Approved for distribution stub only.",
            }
            with TestClient(app) as client:
                unauth = client.post(f"/jobs/{job_id}/approve", json={"approved_by": "x"})
                assert unauth.status_code == 401
                r = client.post(
                    f"/jobs/{job_id}/approve",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"approved_by": "api-user", "enqueue_distribute": False},
                )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["public_post"] is False
    mock_approve.assert_awaited_once()


@pytest.mark.asyncio
async def test_cli_approve(pg_pool, monkeypatch):
    from app.services import jobs
    from app.state_machine import advance
    from app.db import transaction
    from app import cli as cli_mod

    # Do not tear down the shared test pool when CLI finishes
    async def _noop_close():
        return None

    monkeypatch.setattr(cli_mod, "close", _noop_close)
    monkeypatch.setattr(cli_mod, "connect", AsyncMock())

    row = await jobs.create_job(topic="cli approve")
    job_id = row["id"]
    async with transaction() as conn:
        await conn.execute("UPDATE jobs SET status='rendering' WHERE id=$1", job_id)
        await advance(conn, job_id, "rendered", {})
        await advance(conn, job_id, "staged", {})

    rc = await cli_mod.cmd_approve(str(job_id), "cli-user", enqueue_distribute=False, note="lgtm")
    assert rc == 0
    async with pg_pool.acquire() as conn:
        j = await conn.fetchrow("SELECT status, approved_by FROM jobs WHERE id=$1", job_id)
    assert j["status"] == "approved"
    assert j["approved_by"] == "cli-user"

    # CLI refuses non-staged
    rc2 = await cli_mod.cmd_approve(str(job_id), "cli-user", enqueue_distribute=False, note=None)
    assert rc2 == 1


@pytest.mark.asyncio
@pytest.mark.soft_image_gate
async def test_dual_format_no_second_heygen_by_default(pg_pool, monkeypatch):
    from app.services import pipeline, jobs
    from app.services.heygen import HeyGenVideo
    from app.config import settings

    monkeypatch.setattr(settings, "heygen_allow_dual_format", False)
    row = await jobs.create_job(topic="dual format", include_horizontal=True)
    job_id = row["id"]

    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return HeyGenVideo(video_id=f"vid_{len(calls)}", status="processing")

    with patch("app.services.pipeline.heygen.create_video", AsyncMock(side_effect=fake_create)):
        result = await pipeline.generate_avatar(job_id, include_horizontal=True)

    assert len(calls) == 1
    assert calls[0]["dimension"] == (1080, 1920)
    assert result["horizontal_heygen_paid"] is False

    async with pg_pool.acquire() as conn:
        meta = await conn.fetchval("SELECT meta FROM jobs WHERE id=$1", job_id)
        if isinstance(meta, str):
            meta = json.loads(meta)
        h = meta["formats"]["horizontal"]
        video_h = await conn.fetchrow(
            "SELECT * FROM assets WHERE job_id=$1 AND kind='video_h'", job_id
        )
    assert h["heygen_paid"] is False
    assert h["mode"] == "derived"
    assert video_h is None


@pytest.mark.asyncio
@pytest.mark.soft_image_gate
async def test_dual_format_paid_when_env_allows(pg_pool, monkeypatch):
    from app.services import pipeline, jobs
    from app.services.heygen import HeyGenVideo
    from app.config import settings

    monkeypatch.setattr(settings, "heygen_allow_dual_format", True)
    row = await jobs.create_job(topic="paid dual", include_horizontal=True)
    job_id = row["id"]

    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return HeyGenVideo(video_id=f"vid_{len(calls)}", status="processing")

    with patch("app.services.pipeline.heygen.create_video", AsyncMock(side_effect=fake_create)):
        result = await pipeline.generate_avatar(job_id, include_horizontal=True)

    assert len(calls) == 2
    dims = sorted(c["dimension"] for c in calls)
    assert dims == [(1080, 1920), (1920, 1080)]
    assert result["horizontal_heygen_paid"] is True

    async with pg_pool.acquire() as conn:
        video_h = await conn.fetchrow(
            "SELECT * FROM assets WHERE job_id=$1 AND kind='video_h'", job_id
        )
    assert video_h is not None
