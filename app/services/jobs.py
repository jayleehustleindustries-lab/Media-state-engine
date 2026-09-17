from uuid import UUID
import json
from typing import Any
from ..db import transaction
from ..state_machine import advance

async def create_job(script_text: str):
    async with transaction() as conn:
        return await conn.fetchrow('INSERT INTO jobs(script_text) VALUES($1) RETURNING *', script_text)

async def get_job(job_id: UUID):
    async with transaction() as conn:
        return await conn.fetchrow('SELECT * FROM jobs WHERE id=$1', job_id)

async def advance_job(job_id: UUID, to_status: str, payload: dict[str, Any]):
    async with transaction() as conn:
        return await advance(conn, job_id, to_status, payload)

async def add_asset(job_id: UUID, kind: str, url: str | None, storage_path: str | None, meta: dict[str, Any]):
    async with transaction() as conn:
        return await conn.fetchrow('INSERT INTO assets(job_id,kind,url,storage_path,meta) VALUES($1,$2,$3,$4,$5::jsonb) RETURNING *', job_id, kind, url, storage_path, json.dumps(meta))

async def reserve_key(conn, key: str, job_id: UUID, step: str):
    return await conn.fetchrow('INSERT INTO idempotency_keys(key,job_id,step) VALUES($1,$2,$3) ON CONFLICT (key) DO NOTHING RETURNING *', key, job_id, step)

async def get_key(conn, key: str):
    return await conn.fetchrow('SELECT * FROM idempotency_keys WHERE key=$1', key)
