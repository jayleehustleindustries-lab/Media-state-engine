from uuid import UUID
import json
from typing import Any
from ..db import transaction
from ..state_machine import advance
from ..config import settings


def _serialize_row(row) -> dict:
    if row is None:
        return None
    out = dict(row)
    for k, v in list(out.items()):
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif k in ("id", "job_id") and v is not None:
            out[k] = str(v)
        elif k == "meta" and isinstance(v, str):
            out[k] = json.loads(v)
        elif k == "payload" and isinstance(v, str):
            out[k] = json.loads(v)
        elif k == "result" and isinstance(v, str):
            out[k] = json.loads(v)
    return out


async def create_job(script_text: str):
    async with transaction() as conn:
        return await conn.fetchrow('INSERT INTO jobs(script_text) VALUES($1) RETURNING *', script_text)


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
    return {
        'job': _serialize_row(job),
        'status': job['status'],
        'assets': [_serialize_row(a) for a in assets],
        'events': [_serialize_row(e) for e in events],
        'status_history': status_history,
        'work': [_serialize_row(w) for w in work],
        'outbox': [_serialize_row(o) for o in outbox_rows],
    }


async def advance_job(job_id: UUID, to_status: str, payload: dict[str, Any]):
    async with transaction() as conn:
        return await advance(conn, job_id, to_status, payload)


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
