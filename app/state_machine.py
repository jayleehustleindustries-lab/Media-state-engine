from typing import Any
import json
from uuid import UUID

TRANSITIONS = {
    'pending': {'script_ready', 'failed'},
    'script_ready': {'audio_generating', 'failed'},
    'audio_generating': {'audio_ready', 'failed'},
    'audio_ready': {'rendering', 'failed'},
    'rendering': {'rendered', 'failed'},
    'rendered': {'delivered', 'failed'},
    'delivered': set(), 'failed': set(),
}

class IllegalTransition(ValueError):
    pass

async def advance(conn, job_id: UUID, to_status: str, payload: dict[str, Any] | None = None):
    payload = payload or {}
    job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
    if not job:
        raise LookupError('job not found')
    current = job['status']
    if to_status not in TRANSITIONS[current]:
        raise IllegalTransition(f'{current} -> {to_status} is not allowed')
    await conn.execute('INSERT INTO events(job_id, from_status, to_status, payload) VALUES($1,$2,$3,$4::jsonb)', job_id, current, to_status, json.dumps(payload))
    return await conn.fetchrow('UPDATE jobs SET status=$2, updated_at=now() WHERE id=$1 RETURNING *', job_id, to_status)
