"""End-to-end job lifecycle through the REAL pipeline.py functions, against
the REAL canonical schema (no tests/_sot_app_compat.sql overlay).

This is the regression test for the bug found in Phase 25/26: every
pipeline.py `advance()` call site uses legacy-only status names
('rendering', 'audio_generating', ...) and jumps directly between statuses
that canonical requires a real intermediate 'queued' hop for (draft ->
render_queued -> render_running, not draft -> render_running). This was
invisible to every other test file because tests/_sot_app_compat.sql drops
transition_job before they run, silently routing them onto the legacy
graph regardless of what schema the test author thought they were
exercising — and even test_canonical_approval.py, which DOES use the real
canonical schema, never caught this because it drives status changes via
state_machine.transition() directly with pre-computed correct targets, not
via pipeline.py's generate_vertex_clips/render_vertex/distribute the way a
real job actually would.

This file drives a job through generate_vertex_clips -> reconcile_vertex_clips
-> render_vertex -> approve_job -> distribute, with only the external
boundaries mocked (Vertex HTTP calls, the ffmpeg subprocess, and the
distribute-channel calls) — the state machine itself is exercised for real,
against the real transition_job RPC, with zero compat overlay to hide a
break.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
async def canonical_pool(require_database_url):
    import asyncpg
    from app import db as dbmod
    from tests.conftest import apply_canonical_schema

    await dbmod.close()
    pool = await asyncpg.create_pool(require_database_url, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await apply_canonical_schema(conn)
    dbmod._pool = pool
    yield pool
    await pool.close()
    dbmod._pool = None


async def _fake_gate_pass(conn, job_id):
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "verdict": "pass",
        "overall": 9,
        "identity_likeness": 9,
        "ref_content_hash": "test-ref-hash",
    }


class _FakeOperation:
    def __init__(self, operation_name, done=False, video_bytes_b64=None, mime_type=None, error=None):
        self.operation_name = operation_name
        self.done = done
        self.video_bytes_b64 = video_bytes_b64
        self.video_gcs_uri = None
        self.mime_type = mime_type
        self.error = error


@pytest.mark.asyncio
async def test_full_lifecycle_via_real_pipeline_functions_on_canonical(canonical_pool, monkeypatch):
    """draft -> render_queued -> render_running -> render_done -> review ->
    approved -> published, driven entirely through pipeline.py's real
    generate_vertex_clips/reconcile_vertex_clips/render_vertex/distribute —
    the exact functions a real job actually calls, not a hand-picked
    sequence of correct targets."""
    import base64

    from app.services import jobs, pipeline
    from app.services import vertex as vertex_mod
    from app.services import ffmpeg_render as ffmpeg_mod
    from app.services import distribute as distribute_mod
    from app.db import transaction

    monkeypatch.setattr(pipeline, "assert_pass_for_heygen", _fake_gate_pass)

    async def fake_start_clip_generation(*, job_id, prompt, clip_index, reference_images=None, **kw):
        return _FakeOperation(operation_name=f"op-{clip_index}", done=False)

    fake_video_bytes = base64.b64encode(b"not-a-real-mp4-but-nonempty").decode("ascii")

    async def fake_get_clip_operation(operation_name):
        return _FakeOperation(
            operation_name=operation_name, done=True,
            video_bytes_b64=fake_video_bytes, mime_type="video/mp4",
        )

    async def fake_render(job, clip_paths, **kw):
        return {"url": None, "storage_path": "final/fake.mp4", "meta": {"provider": "ffmpeg", "clip_count": len(clip_paths)}}

    async def fake_run_distribution(*, job_id, job_row, final_asset):
        return {
            "mode": "staging_only", "public_post": False, "staging_path": "data/staging/fake.mp4",
            "results": [], "documented_only": True, "platform_captions": {},
        }

    monkeypatch.setattr(vertex_mod, "start_clip_generation", fake_start_clip_generation)
    monkeypatch.setattr(vertex_mod, "get_clip_operation", fake_get_clip_operation)
    monkeypatch.setattr(ffmpeg_mod, "render", fake_render)
    monkeypatch.setattr(distribute_mod, "run_distribution", fake_run_distribution)

    row = await jobs.create_job(script_text="Hook line! Core lift body. CTA now.")
    job_id = row["id"]
    assert row["status"] == "draft"

    # 1. generate_vertex_clips: draft -> render_queued -> render_running
    #    (the exact hop the bug skipped). Must NOT raise IllegalTransition.
    gen_result = await pipeline.generate_vertex_clips(job_id)
    assert gen_result["status"] == "rendering"
    assert gen_result["clip_count"] == 3

    async with canonical_pool.acquire() as conn:
        status_after_gen = await conn.fetchval("SELECT status FROM jobs WHERE id=$1", job_id)
    assert status_after_gen == "render_running"

    # 2. reconcile_vertex_clips: polls the (fake) LROs, downloads bytes,
    #    triggers render_vertex once all 3 are done -> render_running -> review.
    reconcile_result = await pipeline.reconcile_vertex_clips(job_id)
    assert reconcile_result.get("status") in ("staged", "review"), reconcile_result

    async with canonical_pool.acquire() as conn:
        status_after_render = await conn.fetchval("SELECT status FROM jobs WHERE id=$1", job_id)
    assert status_after_render == "review"

    # 3. approve_job: review -> approved, via the REAL canonical approval gate
    #    fixed in Phase 22/25 (also merged onto this branch for this test).
    approve_result = await jobs.approve_job(job_id, approved_by="lifecycle-test-operator", enqueue_distribute=False)
    assert approve_result["status"] == "approved"

    async with canonical_pool.acquire() as conn:
        status_after_approve = await conn.fetchval("SELECT status FROM jobs WHERE id=$1", job_id)
    assert status_after_approve == "approved"

    # 4. distribute: approved -> delivered (canonical: published).
    dist_result = await pipeline.distribute(job_id)
    assert dist_result["status"] == "delivered"

    async with canonical_pool.acquire() as conn:
        final_status = await conn.fetchval("SELECT status FROM jobs WHERE id=$1", job_id)
        events = await conn.fetch(
            "SELECT from_status, to_status FROM job_events WHERE job_id=$1 ORDER BY id", job_id,
        )
    assert final_status == "published"
    # Confirm the real hop sequence actually happened, including the
    # previously-skipped queued precursor.
    hops = [(e["from_status"], e["to_status"]) for e in events]
    assert ("draft", "render_queued") in hops
    assert ("render_queued", "render_running") in hops
    assert ("render_running", "render_done") in hops
    assert ("render_done", "review") in hops
    assert ("review", "approved") in hops
    assert ("approved", "published") in hops


@pytest.mark.asyncio
async def test_advance_inserts_prestep_only_when_needed(canonical_pool):
    """A job already sitting at the queued precursor must NOT get a
    redundant/illegal duplicate hop — advance() should go straight to the
    running target."""
    from app.services import jobs
    from app.state_machine import advance, transition
    from app.db import transaction

    row = await jobs.create_job(script_text="Hook body cta.")
    job_id = row["id"]

    async with transaction() as conn:
        await transition(conn, job_id, "render_queued", require_quality_pass=False, require_image_pass=False)
        # Legacy-style call, as pipeline.py actually makes it. Must resolve
        # to a single hop (render_queued -> render_running), not attempt to
        # re-enter render_queued from render_queued (illegal self-loop).
        result = await advance(conn, job_id, "rendering", {})
    assert result["status"] == "render_running"
