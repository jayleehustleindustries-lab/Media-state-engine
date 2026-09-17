"""Postgres-backed durable work queue (Phase 2).

Chosen over RQ/Celery because this stack already has Postgres + FOR UPDATE
SKIP LOCKED patterns (see outbox). No Redis/broker required.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from ..config import settings

STEPS = frozenset({
    "score_image",
    "revise_image",
    "generate_audio",
    "generate_avatar",
    "render",
    "reconcile",
    "flush_outbox",
    "distribute",
})


async def enqueue(
    conn,
    job_id: UUID,
    step: str,
    payload: dict[str, Any] | None = None,
    *,
    max_attempts: int | None = None,
) -> dict:
    """Insert a work item, or return the existing active one for (job_id, step)."""
    if step not in STEPS:
        raise ValueError(f"unknown work step: {step}")
    payload = payload or {}
    max_attempts = max_attempts if max_attempts is not None else settings.worker_max_attempts

    existing = await conn.fetchrow(
        """
        SELECT * FROM work_queue
        WHERE job_id = $1 AND step = $2 AND status IN ('pending', 'running')
        ORDER BY id DESC
        LIMIT 1
        """,
        job_id,
        step,
    )
    if existing:
        return dict(existing)

    row = await conn.fetchrow(
        """
        INSERT INTO work_queue(job_id, step, payload, max_attempts)
        VALUES($1, $2, $3::jsonb, $4)
        RETURNING *
        """,
        job_id,
        step,
        json.dumps(payload),
        max_attempts,
    )
    return dict(row)


async def enqueue_step(
    job_id: UUID,
    step: str,
    payload: dict[str, Any] | None = None,
) -> dict:
    """Enqueue outside an existing transaction (validates job exists)."""
    from ..db import transaction

    async with transaction() as conn:
        job = await conn.fetchrow("SELECT id, status FROM jobs WHERE id=$1", job_id)
        if not job:
            raise LookupError("job not found")
        work = await enqueue(conn, job_id, step, payload)
        work["job_status"] = job["status"]
        return work


async def claim_due(conn, limit: int = 5) -> list[dict]:
    """Lock and mark due pending rows as running; return claimed rows."""
    rows = await conn.fetch(
        """
        WITH due AS (
          SELECT id FROM work_queue
          WHERE status = 'pending' AND next_attempt_at <= now()
          ORDER BY next_attempt_at, id
          FOR UPDATE SKIP LOCKED
          LIMIT $1
        )
        UPDATE work_queue w
        SET status = 'running',
            attempts = attempts + 1,
            started_at = COALESCE(started_at, now()),
            updated_at = now()
        FROM due WHERE w.id = due.id
        RETURNING w.*
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def mark_done(conn, work_id: int, result: dict[str, Any] | None = None) -> None:
    payload_patch = json.dumps({"result": result} if result is not None else {})
    await conn.execute(
        """
        UPDATE work_queue
        SET status = 'done',
            finished_at = now(),
            updated_at = now(),
            last_error = NULL,
            payload = CASE
              WHEN $2::text = '{}'::text THEN payload
              ELSE payload || $2::jsonb
            END
        WHERE id = $1
        """,
        work_id,
        payload_patch,
    )


async def mark_failure(conn, work_id: int, error: str, attempts: int, max_attempts: int) -> str:
    """Schedule retry or dead-letter. Returns new status."""
    if attempts >= max_attempts:
        await conn.execute(
            """
            UPDATE work_queue
            SET status = 'dead',
                last_error = $2,
                finished_at = now(),
                updated_at = now()
            WHERE id = $1
            """,
            work_id,
            error[:2000],
        )
        return "dead"
    # Exponential backoff similar to outbox
    delay = min(settings.webhook_outbox_base_delay_seconds * (2 ** max(attempts - 1, 0)), 3600.0)
    await conn.execute(
        """
        UPDATE work_queue
        SET status = 'pending',
            last_error = $2,
            next_attempt_at = now() + ($3 || ' seconds')::interval,
            updated_at = now()
        WHERE id = $1
        """,
        work_id,
        error[:2000],
        str(delay),
    )
    return "pending"


async def get_work(work_id: int) -> dict | None:
    from ..db import transaction

    async with transaction() as conn:
        row = await conn.fetchrow("SELECT * FROM work_queue WHERE id=$1", work_id)
        return dict(row) if row else None


async def list_for_job(conn, job_id: UUID) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT * FROM work_queue
        WHERE job_id = $1
        ORDER BY created_at, id
        """,
        job_id,
    )
    return [dict(r) for r in rows]


def serialize_work(row: dict) -> dict:
    out = dict(row)
    for k, v in list(out.items()):
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif k == "job_id":
            out[k] = str(v)
        elif k == "payload" and isinstance(v, str):
            out[k] = json.loads(v)
    return out


async def reclaim_stale_running(
    conn,
    *,
    older_than_seconds: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """Reset stuck ``running`` rows to ``pending`` after a worker crash (audit F4).

    Attempts are left as-is (already incremented on claim); next claim bumps again.
    ``next_attempt_at`` is set to now so the item is immediately claimable.
    """
    older = int(older_than_seconds if older_than_seconds is not None else settings.work_queue_stale_seconds)
    rows = await conn.fetch(
        """
        WITH stale AS (
          SELECT id FROM work_queue
          WHERE status = 'running'
            AND COALESCE(started_at, updated_at) < now() - ($1 || ' seconds')::interval
          ORDER BY COALESCE(started_at, updated_at), id
          FOR UPDATE SKIP LOCKED
          LIMIT $2
        )
        UPDATE work_queue w
        SET status = 'pending',
            next_attempt_at = now(),
            updated_at = now(),
            last_error = left(
              CONCAT_WS(' | ', NULLIF(last_error, ''), 'reclaimed from stale running'),
              2000
            )
        FROM stale WHERE w.id = stale.id
        RETURNING w.*
        """,
        str(older),
        limit,
    )
    return [dict(r) for r in rows]

