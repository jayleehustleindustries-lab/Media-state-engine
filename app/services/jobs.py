from uuid import UUID
import json
from typing import Any
from ..db import transaction
from ..state_machine import advance
from ..config import settings
from . import scriptgen


def _parse_json_field(v):
    if isinstance(v, str):
        return json.loads(v)
    return v


def _serialize_row(row) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for k, v in list(out.items()):
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif k in ("id", "job_id") and v is not None:
            out[k] = str(v)
        elif k in ("meta", "script", "payload", "result") and isinstance(v, str):
            out[k] = json.loads(v)
    return out


async def create_job(
    script_text: str | None = None,
    *,
    topic: str | None = None,
    duration_target_seconds: int = 30,
    cta: str | None = None,
    include_horizontal: bool = False,
    platforms: list[str] | None = None,
    auto_script_ready: bool = True,
    heygen_dual_paid: bool | None = None,
):
    """Create a job with structured script + platform caption staging fields."""
    dual = bool(settings.heygen_allow_dual_format) if heygen_dual_paid is None else bool(heygen_dual_paid)
    # Cost guardrail: paid dual only when env allow-flag AND caller asked for horizontal
    paid_horizontal = bool(include_horizontal) and bool(settings.heygen_allow_dual_format) and dual
    full_text, script, meta = scriptgen.compose_script(
        script_text=script_text,
        topic=topic,
        duration_target_seconds=duration_target_seconds,
        cta=cta,
        include_horizontal=include_horizontal,
        heygen_dual_paid=paid_horizontal,
        platforms=platforms,
    )
    async with transaction() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO jobs(script_text, script, meta, status)
            VALUES($1, $2::jsonb, $3::jsonb, 'pending')
            RETURNING *
            """,
            full_text,
            json.dumps(script),
            json.dumps(meta),
        )
        if auto_script_ready:
            row = await advance(conn, row['id'], 'script_ready', {
                'script': script,
                'formats': meta.get('formats'),
                'platform_captions_staged': True,
            })
        return row


async def get_job(job_id: UUID):
    async with transaction() as conn:
        return await conn.fetchrow('SELECT * FROM jobs WHERE id=$1', job_id)


async def get_job_detail(job_id: UUID) -> dict | None:
    """Job row plus assets, events (status history), and work_queue items."""
    async with transaction() as conn:
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1', job_id)
        if not job:
            return None
        assets = await conn.fetch(
            'SELECT * FROM assets WHERE job_id=$1 ORDER BY created_at, id', job_id
        )
        events = await conn.fetch(
            'SELECT * FROM events WHERE job_id=$1 ORDER BY created_at, id', job_id
        )
        work = await conn.fetch(
            'SELECT * FROM work_queue WHERE job_id=$1 ORDER BY created_at, id', job_id
        )
        outbox_rows = await conn.fetch(
            'SELECT * FROM webhook_outbox WHERE job_id=$1 ORDER BY created_at, id', job_id
        )
    status_history = [
        {
            'from_status': e['from_status'],
            'to_status': e['to_status'],
            'payload': e['payload'] if not isinstance(e['payload'], str) else json.loads(e['payload']),
            'created_at': e['created_at'].isoformat() if hasattr(e['created_at'], 'isoformat') else e['created_at'],
            'event_id': e['id'],
        }
        for e in events
    ]
    job_out = _serialize_row(job)
    return {
        'job': job_out,
        'status': job['status'],
        'script': _parse_json_field(job['script']) if 'script' in job.keys() else {},
        'meta': _parse_json_field(job['meta']) if 'meta' in job.keys() else {},
        'awaiting_approval': job['status'] == 'staged',
        'approved': job['status'] in ('approved', 'delivered'),
        'assets': [_serialize_row(a) for a in assets],
        'events': [_serialize_row(e) for e in events],
        'status_history': status_history,
        'work': [_serialize_row(w) for w in work],
        'outbox': [_serialize_row(o) for o in outbox_rows],
    }


async def advance_job(job_id: UUID, to_status: str, payload: dict[str, Any]):
    # Hard gate: never allow skipping approval into delivered
    if to_status == 'delivered':
        async with transaction() as conn:
            job = await conn.fetchrow('SELECT status FROM jobs WHERE id=$1', job_id)
            if not job:
                raise LookupError('job not found')
            if job['status'] != 'approved':
                raise ValueError(
                    f'cannot deliver from {job["status"]}: explicit approve required '
                    f'(status must be approved before delivered / any public post)'
                )
    async with transaction() as conn:
        return await advance(conn, job_id, to_status, payload)


async def approve_job(
    job_id: UUID,
    *,
    approved_by: str | None = None,
    enqueue_distribute: bool = True,
    note: str | None = None,
) -> dict:
    """Flip staged → approved. Optionally enqueue distribute stub (no public post)."""
    from . import queue

    async with transaction() as conn:
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job:
            raise LookupError('job not found')
        if job['status'] != 'staged':
            raise ValueError(
                f'job status is {job["status"]}, expected staged '
                f'(nothing auto-publishes; approve only from staged)'
            )
        who = (approved_by or 'operator').strip() or 'operator'
        await conn.execute(
            """
            UPDATE jobs
            SET approved_at = now(),
                approved_by = $2,
                meta = COALESCE(meta, '{}'::jsonb) || $3::jsonb,
                updated_at = now()
            WHERE id = $1
            """,
            job_id,
            who,
            json.dumps({
                'awaiting_approval': False,
                'approval_note': note,
                'auto_publish': False,
            }),
        )
        row = await advance(conn, job_id, 'approved', {
            'approved_by': who,
            'note': note,
            'via': 'approve_gate',
        })
        work = None
        if enqueue_distribute:
            work = await queue.enqueue(conn, job_id, 'distribute', {
                'stub': True,
                'approved_by': who,
                'note': 'Phase 3 distribute stub — no public platform post',
            })
    return {
        'job': _serialize_row(row),
        'status': 'approved',
        'approved_by': who,
        'distribute_enqueued': work is not None,
        'work_id': int(work['id']) if work else None,
        'public_post': False,
        'note': 'Approved for distribution stub only. Phase 4 wires real platform publish.',
    }


async def add_asset(job_id: UUID, kind: str, url: str | None, storage_path: str | None, meta: dict[str, Any]):
    async with transaction() as conn:
        return await conn.fetchrow(
            'INSERT INTO assets(job_id,kind,url,storage_path,meta) VALUES($1,$2,$3,$4,$5::jsonb) RETURNING *',
            job_id, kind, url, storage_path, json.dumps(meta),
        )


async def reserve_key(conn, key: str, job_id: UUID, step: str):
    ttl = int(settings.idempotency_ttl_seconds)
    return await conn.fetchrow(
        """
        INSERT INTO idempotency_keys(key, job_id, step, expires_at)
        VALUES($1, $2, $3, now() + ($4 || ' seconds')::interval)
        ON CONFLICT (key) DO NOTHING
        RETURNING *
        """,
        key, job_id, step, str(ttl),
    )


async def get_key(conn, key: str):
    """Return a live idempotency row. Expired unfinished keys are deleted (poison recovery)."""
    row = await conn.fetchrow('SELECT * FROM idempotency_keys WHERE key=$1', key)
    if not row:
        return None
    # Finished keys (result set) are always returned — they block double-spend forever.
    if row['result'] is not None:
        return row
    # Unfinished + past expires_at → clear so a retry can reserve a fresh key.
    expired = await conn.fetchrow(
        """
        DELETE FROM idempotency_keys
        WHERE key = $1 AND result IS NULL
          AND expires_at IS NOT NULL AND expires_at < now()
        RETURNING *
        """,
        key,
    )
    if expired:
        return None
    return row


async def clear_key(conn, key: str) -> None:
    """Remove an idempotency key so a failed step can be retried safely."""
    await conn.execute('DELETE FROM idempotency_keys WHERE key=$1', key)


async def clear_keys_for_job_step(conn, job_id: UUID, step: str) -> None:
    await conn.execute('DELETE FROM idempotency_keys WHERE job_id=$1 AND step=$2', job_id, step)
