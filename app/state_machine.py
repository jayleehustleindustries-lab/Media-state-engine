from typing import Any
import json
from uuid import UUID

# Phase 3 status machine:
#   ... → rendering → rendered → staged → approved → delivered
# Public platform distribution is ONLY allowed after explicit approve (staged → approved).
# There is NO edge from staged/rendered directly to delivered.
TRANSITIONS = {
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

# Statuses that mean "waiting on a human before any public post"
AWAITING_APPROVAL = frozenset({'staged'})
# Statuses from which public distribution may be enqueued
DISTRIBUTABLE = frozenset({'approved'})


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
    await conn.execute(
        'INSERT INTO events(job_id, from_status, to_status, payload) VALUES($1,$2,$3,$4::jsonb)',
        job_id, current, to_status, json.dumps(payload),
    )
    return await conn.fetchrow(
        'UPDATE jobs SET status=$2, updated_at=now() WHERE id=$1 RETURNING *',
        job_id, to_status,
    )
