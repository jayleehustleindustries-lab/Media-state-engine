"""Canonical-schema approval gate — exercised against the REAL transition_job RPC.

Unlike every other Postgres-backed test file in this suite, this one does
NOT apply tests/_sot_app_compat.sql. It applies ONLY
supabase/migrations/*.sql (see tests.conftest.apply_canonical_schema),
so transition_job / the job_status enum / the jobs table are exactly what
a real Supabase deploy would have — no legacy status values, no `script`
or `events` compat tables, no dropped RPC.

This is the real regression coverage for the bug this fix addresses:
create_job() used to INSERT status='pending' (not a legal job_status
value here) referencing a nonexistent `script` column, and approve_job()
checked `status != 'staged'` (canonical jobs are never 'staged' — they
reach 'review') and UPDATEd nonexistent approved_at/approved_by columns.
Both would have failed loudly the moment this file ran them against a
real canonical database; before this fix, nothing in the test suite ever
did that, because tests/_sot_app_compat.sql deleted transition_job for
every other test file (see the comment there for why that DROP stays,
scoped to the pipeline/HeyGen/image-gate code this fix does not touch).
"""
from __future__ import annotations

import hashlib
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


async def _confirm_real_rpc_is_live(conn):
    """Sanity check the fixture itself: transition_job must actually be present."""
    present = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname='transition_job')"
    )
    assert present is True, "canonical_pool fixture did not leave transition_job live"
    has_pending = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid "
        "WHERE t.typname='job_status' AND e.enumlabel='pending')"
    )
    assert has_pending is False, "canonical_pool fixture leaked a legacy enum value"


async def _drive_to_review(conn, job_id):
    """draft -> render_queued -> render_running -> render_done -> review.

    Uses state_machine.transition() directly (not the approve_job() this
    fix owns) with the quality/image gates explicitly bypassed — those
    gates are separate, out-of-scope subsystems; this only needs a job
    sitting in 'review', which is the real precondition approve_job()
    must handle correctly.
    """
    from app.state_machine import transition

    await transition(conn, job_id, 'render_queued', require_quality_pass=False, require_image_pass=False)
    await transition(conn, job_id, 'render_running', require_quality_pass=False, require_image_pass=False)
    await transition(conn, job_id, 'render_done', require_quality_pass=False, require_image_pass=False)
    await transition(conn, job_id, 'review', require_quality_pass=False, require_image_pass=False)


@pytest.mark.asyncio
async def test_create_job_is_valid_on_canonical_schema(canonical_pool):
    """The literal bug: INSERT status='pending' + `script` column both
    don't exist on canonical — create_job() must not reference either."""
    from app.services import jobs

    async with canonical_pool.acquire() as conn:
        await _confirm_real_rpc_is_live(conn)

    row = await jobs.create_job(topic="canonical smoke test", duration_target_seconds=30)
    assert row['status'] == 'draft'  # legal job_status default, not 'pending'

    meta = row['meta']
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta['script']['hook']
    assert meta['script']['duration_target_seconds'] == 30
    assert 'platform_captions' in meta
    assert meta['awaiting_approval'] is True

    async with canonical_pool.acquire() as conn:
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='jobs'"
        )
    col_names = {c['column_name'] for c in cols}
    assert 'script' not in col_names  # confirms we're really on canonical-only schema


@pytest.mark.asyncio
async def test_approve_job_succeeds_via_real_transition_job_rpc(canonical_pool):
    from app.services import jobs
    from app.db import transaction

    row = await jobs.create_job(script_text="Hook! Body does the real work. Follow now.")
    job_id = row['id']

    async with transaction() as conn:
        await _drive_to_review(conn, job_id)
        status = await conn.fetchval('SELECT status FROM jobs WHERE id=$1', job_id)
    assert status == 'review'

    result = await jobs.approve_job(job_id, approved_by='trusted-operator', enqueue_distribute=False)
    assert result['status'] == 'approved'
    assert result['approved_by'] == 'trusted-operator'

    async with canonical_pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT status, approved_at, approved_by, approved_by_key_id FROM jobs WHERE id=$1',
            job_id,
        )
        events = await conn.fetch(
            "SELECT from_status, to_status, actor FROM job_events WHERE job_id=$1 ORDER BY id",
            job_id,
        )
    assert job['status'] == 'approved'
    assert job['approved_at'] is not None
    assert job['approved_by'] == 'trusted-operator'
    assert job['approved_by_key_id'] is None  # no verified api_keys row was involved
    # The real RPC wrote the audit trail — this is job_events, the canonical
    # table, not the legacy `events` table (which doesn't exist here).
    assert any(e['from_status'] == 'review' and e['to_status'] == 'approved' for e in events)


@pytest.mark.asyncio
async def test_approve_job_rejects_second_approve(canonical_pool):
    from app.services import jobs
    from app.db import transaction

    row = await jobs.create_job(script_text="Hook! Body. CTA now.")
    job_id = row['id']
    async with transaction() as conn:
        await _drive_to_review(conn, job_id)

    await jobs.approve_job(job_id, approved_by='first-approver', enqueue_distribute=False)

    with pytest.raises(ValueError, match='review'):
        await jobs.approve_job(job_id, approved_by='second-approver', enqueue_distribute=False)

    async with canonical_pool.acquire() as conn:
        job = await conn.fetchrow('SELECT status, approved_by FROM jobs WHERE id=$1', job_id)
    # Second (rejected) attempt must not have clobbered the first approval.
    assert job['status'] == 'approved'
    assert job['approved_by'] == 'first-approver'


@pytest.mark.asyncio
async def test_approve_job_rejects_wrong_status(canonical_pool):
    from app.services import jobs

    row = await jobs.create_job(script_text="Still a draft, never rendered.")
    job_id = row['id']
    assert row['status'] == 'draft'

    with pytest.raises(ValueError, match='draft'):
        await jobs.approve_job(job_id, approved_by='too-early', enqueue_distribute=False)


@pytest.mark.asyncio
async def test_approve_job_rejects_illegal_rpc_transition(canonical_pool):
    """The real RPC's own legal-transition graph — not just approve_job's
    status precheck — must refuse a skip-ahead transition."""
    from app.services import jobs
    from app.state_machine import IllegalTransition
    from app.db import transaction

    row = await jobs.create_job(script_text="Draft only.")
    job_id = row['id']

    with pytest.raises(IllegalTransition):
        async with transaction() as conn:
            from app.state_machine import transition
            await transition(conn, job_id, 'approved', require_quality_pass=False, require_image_pass=False)


@pytest.mark.asyncio
async def test_approve_job_actor_unverified_fallback_is_not_client_controlled(canonical_pool):
    """No api_keys row matches -> approved_by must come from a hash of the
    presented credential, never an arbitrary caller-chosen string."""
    from app.services import jobs
    from app.db import transaction

    row = await jobs.create_job(script_text="Hook body cta.")
    job_id = row['id']
    async with transaction() as conn:
        await _drive_to_review(conn, job_id)

    presented = 'shared-env-secret-not-in-api-keys-table'
    result = await jobs.approve_job(job_id, approved_by_key=presented, enqueue_distribute=False)

    expected = 'apikey:' + hashlib.sha256(presented.encode()).hexdigest()[:12]
    assert result['approved_by'] == expected

    async with canonical_pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT approved_by, approved_by_key_id, meta FROM jobs WHERE id=$1', job_id
        )
    assert job['approved_by'] == expected
    assert job['approved_by_key_id'] is None
    meta = json.loads(job['meta']) if isinstance(job['meta'], str) else job['meta']
    assert meta['approval_identity_source'] == 'shared_secret_unverified'


@pytest.mark.asyncio
async def test_approve_job_actor_resolves_verified_api_key_identity(canonical_pool):
    """A real api_keys row DOES verifiably resolve — the strong path works,
    and its identity cannot be overridden by a client-supplied approved_by."""
    from app.services import jobs
    from app.db import transaction
    from uuid import uuid4

    raw_key = 'verified-operator-raw-key-0001'
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_id = uuid4()
    async with canonical_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO api_keys(id, name, key_prefix, key_hash, is_active)
            VALUES ($1, 'jordan-ops', $2, $3, true)
            """,
            key_id, raw_key[:8], key_hash,
        )

    row = await jobs.create_job(script_text="Hook body cta.")
    job_id = row['id']
    async with transaction() as conn:
        await _drive_to_review(conn, job_id)

    # approved_by (trusted-caller kwarg) must NOT win over a verified key.
    result = await jobs.approve_job(
        job_id,
        approved_by='ignored-client-supplied-name',
        approved_by_key=raw_key,
        enqueue_distribute=False,
    )
    assert result['approved_by'] == 'jordan-ops'

    async with canonical_pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT approved_by, approved_by_key_id, meta FROM jobs WHERE id=$1', job_id
        )
    assert job['approved_by'] == 'jordan-ops'
    assert str(job['approved_by_key_id']) == str(key_id)
    meta = json.loads(job['meta']) if isinstance(job['meta'], str) else job['meta']
    assert meta['approval_identity_source'] == 'api_keys'
