"""Job status transitions.

Prefer Supabase SoT RPC ``transition_job`` when present. Fall back to the
legacy in-app graph for older schemas (Phase 1–5 tests / rollbacks).

Publish ONLY via approved → published (new) or staged → approved → delivered
(legacy). Image gate statuses exist only on the new graph.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

# ---------------------------------------------------------------------------
# New canonical graph (001+002+004)
# ---------------------------------------------------------------------------
CANONICAL = frozenset({
    'draft',
    'image_queued', 'image_running', 'image_ready', 'image_failed', 'needs_human_review',
    'tts_queued', 'tts_running', 'tts_done', 'tts_failed',
    'render_queued', 'render_running', 'render_done', 'render_failed',
    'review', 'approved', 'published', 'cancelled', 'dead',
})

LEGACY_TO_CANONICAL = {
    'pending': 'draft',
    'script_ready': 'draft',
    'audio_generating': 'tts_running',
    'audio_ready': 'tts_done',
    'rendering': 'render_running',
    'rendered': 'render_done',
    'staged': 'review',
    'approved': 'approved',
    'delivered': 'published',
    'failed': 'dead',
    'published': 'published',
}

# Legacy Phase 1–5 graph (kept for dual-mode / old migrations)
LEGACY_TRANSITIONS = {
    'pending': {'script_ready', 'failed'},
    'script_ready': {'audio_generating', 'rendering', 'failed'},
    'audio_generating': {'audio_ready', 'failed'},
    'audio_ready': {'rendering', 'failed'},
    'rendering': {'rendered', 'failed'},
    'rendered': {'staged', 'failed'},
    'staged': {'approved', 'failed'},
    'approved': {'delivered', 'failed'},
    'delivered': set(),
    'failed': set(),
}

TRANSITIONS = {
    'draft': {'image_queued', 'tts_queued', 'render_queued', 'cancelled'},
    'image_queued': {'image_running', 'image_failed', 'cancelled', 'needs_human_review'},
    'image_running': {'image_ready', 'image_failed', 'needs_human_review', 'cancelled'},
    'image_ready': {'tts_queued', 'render_queued', 'cancelled'},
    'image_failed': {'image_queued', 'needs_human_review', 'dead', 'cancelled'},
    'needs_human_review': {'image_queued', 'draft', 'cancelled', 'dead'},
    'tts_queued': {'tts_running', 'tts_failed', 'cancelled'},
    'tts_running': {'tts_done', 'tts_failed', 'cancelled'},
    'tts_done': {'render_queued', 'cancelled'},
    'tts_failed': {'tts_queued', 'dead', 'cancelled'},
    'render_queued': {'render_running', 'render_failed', 'cancelled'},
    'render_running': {'render_done', 'render_failed', 'cancelled'},
    'render_done': {'review', 'cancelled'},
    'render_failed': {'render_queued', 'dead', 'cancelled'},
    'review': {'approved', 'cancelled', 'render_queued'},
    'approved': {'published', 'cancelled'},
    'published': {'cancelled'},
    'cancelled': {'draft'},
    'dead': {'tts_queued', 'render_queued', 'image_queued', 'cancelled'},
}

AWAITING_APPROVAL = frozenset({'review', 'staged'})
DISTRIBUTABLE = frozenset({'approved'})

# Canonical requires a real queued->running hop that the legacy graph collapsed
# into one status. A legacy-style single-hop advance() (e.g. 'rendering',
# which canonicalizes to 'render_running') must pass through the queued
# precursor first on canonical, or the RPC rejects it as an illegal jump from
# whatever pre-render state the job is actually in (draft/image_ready/tts_done/...).
CANONICAL_PRESTEP = {
    'render_running': 'render_queued',
    'tts_running': 'tts_queued',
    'image_running': 'image_queued',
}


class IllegalTransition(ValueError):
    pass


def canonicalize(status: str) -> str:
    if status in CANONICAL:
        return status
    if status in LEGACY_TO_CANONICAL:
        return LEGACY_TO_CANONICAL[status]
    return status


def status_matches(current: str, target: str) -> bool:
    """True if `current` represents the same logical stage as `target`,
    across legacy/canonical naming (both sides canonicalized before compare).
    Use this instead of a raw `status == 'rendering'` string check anywhere
    that check must hold on both graphs."""
    return canonicalize(current) == canonicalize(target)


async def _has_transition_job(conn) -> bool:
    """True only when new SoT enum+RPC are both live (not legacy schema.sql)."""
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname='transition_job')
           AND EXISTS (SELECT 1 FROM pg_type WHERE typname='job_status')
           AND EXISTS (
             SELECT 1 FROM information_schema.columns
              WHERE table_schema='public' AND table_name='jobs'
                AND column_name='status' AND udt_name='job_status'
           )
        """
    ))


async def _legacy_advance(conn, job_id: UUID, to_status: str, payload: dict[str, Any] | None = None):
    payload = payload or {}
    job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
    if not job:
        raise LookupError('job not found')
    current = job['status']
    allowed = LEGACY_TRANSITIONS.get(current) or TRANSITIONS.get(current) or set()
    if to_status not in allowed:
        raise IllegalTransition(f'{current} -> {to_status} is not allowed')
    # Prefer job_events if present, else events
    has_job_events = await conn.fetchval(
        "SELECT 1 FROM information_schema.tables WHERE table_name='job_events'"
    )
    if has_job_events:
        await conn.execute(
            'INSERT INTO job_events(job_id, from_status, to_status, actor, reason, payload) '
            'VALUES($1,$2,$3,$4,$5,$6::jsonb)',
            job_id, current, to_status, 'system', None, json.dumps(payload),
        )
    has_events = await conn.fetchval(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='events'"
    )
    if has_events:
        await conn.execute(
            'INSERT INTO events(job_id, from_status, to_status, payload) VALUES($1,$2,$3,$4::jsonb)',
            job_id, current, to_status, json.dumps(payload),
        )
    elif not has_job_events:
        raise RuntimeError('neither job_events nor events table present')
    return await conn.fetchrow(
        'UPDATE jobs SET status=$2, updated_at=now() WHERE id=$1 RETURNING *',
        job_id, to_status,
    )


async def transition(
    conn,
    job_id: UUID,
    to_status: str,
    *,
    actor: str = 'system',
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
    set_locked_by: str | None = None,
    require_quality_pass: bool = True,
    require_image_pass: bool = True,
):
    """Preferred API. Uses transition_job RPC when available."""
    payload = payload or {}
    if not await _has_transition_job(conn):
        return await _legacy_advance(conn, job_id, to_status, payload)

    to_status = canonicalize(to_status)
    try:
        row = await conn.fetchrow(
            """
            SELECT * FROM transition_job(
              $1::uuid,
              $2::job_status,
              $3::text,
              $4::text,
              $5::jsonb,
              $6::text,
              $7::text,
              $8::boolean,
              $9::boolean
            )
            """,
            job_id,
            to_status,
            actor,
            reason,
            json.dumps(payload),
            error,
            set_locked_by,
            require_quality_pass,
            require_image_pass,
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if 'illegal transition' in msg or 'P0001' in msg:
            raise IllegalTransition(msg) from exc
        raise
    return row


async def advance(
    conn,
    job_id: UUID,
    to_status: str,
    payload: dict[str, Any] | None = None,
    *,
    require_quality_pass: bool = True,
    require_image_pass: bool = True,
):
    """Legacy wrapper used by older pipeline code.

    On canonical, some legacy single-hop targets (e.g. 'rendering' ->
    canonical 'render_running') actually require a real intermediate
    'queued' hop first (draft -> render_queued -> render_running) that the
    legacy graph never needed. Insert that hop automatically here so every
    existing pipeline.py call site keeps working unmodified on both graphs,
    instead of hand-patching each of the ~11 call sites individually (the
    graph knowledge stays centralized in one place, not scattered).

    require_quality_pass/require_image_pass default True (unchanged prior
    behavior) and forward straight to transition_job's real DB-level gates.
    Vertex's pipeline.py call sites pass require_quality_pass=False
    explicitly as a visible off-switch (no real hook/script quality scorer
    exists yet — task #17), not silently here.
    """
    payload = payload or {}
    if await _has_transition_job(conn):
        canonical_to = canonicalize(to_status)
        prestep = CANONICAL_PRESTEP.get(canonical_to)
        if prestep:
            job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
            current_canonical = canonicalize(job['status']) if job else None
            if current_canonical not in (canonical_to, prestep):
                await transition(
                    conn, job_id, prestep, payload=payload,
                    require_quality_pass=require_quality_pass, require_image_pass=require_image_pass,
                )
    return await transition(
        conn, job_id, to_status, payload=payload,
        require_quality_pass=require_quality_pass, require_image_pass=require_image_pass,
    )
