from uuid import UUID
import hashlib
import json
import secrets
from typing import Any
from ..db import transaction
from ..state_machine import advance, canonicalize, _has_transition_job
from ..config import settings
from . import scriptgen
from .identity import resolve_actor_identity, unverified_identity_label


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
        canonical = await _has_transition_job(conn)
        if canonical:
            # Canonical schema (supabase/migrations/*.sql) has no `script`
            # column and no 'pending' job_status value — the legal initial
            # status is the column default 'draft'. Structured script
            # fields already have a home: `meta` is the established jsonb
            # catch-all scriptgen writes formats/platform_captions/
            # awaiting_approval/auto_publish into, so the hook/body/cta
            # breakdown goes there too rather than a redundant new column.
            meta_to_store = dict(meta)
            meta_to_store['script'] = script
            # jobs.idempotency_key / jobs.script_hash are NOT NULL with no
            # column default on canonical (unlike the legacy-compat
            # overlay, which patches in defaults for exactly this reason
            # — see tests/_sot_app_compat.sql). script_hash mirrors the
            # DB-native create_job() RPC's own hashing (001); this
            # app-level path has no request-level dedup contract of its
            # own, so idempotency_key only needs to be unique per call.
            script_hash = hashlib.sha256(full_text.strip().encode()).hexdigest()
            idempotency_key = f"{script_hash}:{secrets.token_hex(8)}"
            row = await conn.fetchrow(
                """
                INSERT INTO jobs(script_text, meta, status, script_hash, idempotency_key)
                VALUES($1, $2::jsonb, 'draft', $3, $4)
                RETURNING *
                """,
                full_text,
                json.dumps(meta_to_store),
                script_hash,
                idempotency_key,
            )
            # 'draft' already reflects "script composed and staged" for the
            # canonical graph — there is no separate 'script_ready' concept
            # to advance into (canonicalize('script_ready') == 'draft').
        else:
            # Legacy graph (schema.sql / tests/_sot_app_compat.sql overlay).
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
    # metrics outside txn
    from . import metrics as metrics_mod
    try:
        await metrics_mod.incr('jobs_created', labels={'source': 'api'})
    except Exception:
        pass
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
        # Legacy graph (tests/_sot_app_compat.sql) adds assets.job_id so
        # assets are job-scoped by FK-on-the-asset-row. Canonical schema
        # (supabase/migrations/*.sql) has no such column — assets are
        # linked the other way, via jobs.script_asset_id / audio_asset_id
        # / video_asset_id / thumb_asset_id pointing AT an assets row.
        has_asset_job_id = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='assets' AND column_name='job_id'"
        )
        if has_asset_job_id:
            assets = await conn.fetch(
                'SELECT * FROM assets WHERE job_id=$1 ORDER BY created_at, id', job_id
            )
        else:
            asset_ids = [
                job[k] for k in
                ('script_asset_id', 'audio_asset_id', 'video_asset_id', 'thumb_asset_id')
                if k in job.keys() and job[k] is not None
            ]
            assets = await conn.fetch(
                'SELECT * FROM assets WHERE id = ANY($1::uuid[]) ORDER BY created_at, id',
                asset_ids,
            ) if asset_ids else []
        # Legacy graph (tests/_sot_app_compat.sql) has `events`; canonical
        # schema (supabase/migrations/*.sql) only has `job_events` — same
        # table-name split transition_job's own audit insert follows.
        has_events = await conn.fetchval(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='events'"
        )
        events_table = 'events' if has_events else 'job_events'
        events = await conn.fetch(
            f'SELECT * FROM {events_table} WHERE job_id=$1 ORDER BY created_at, id', job_id
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
    meta_parsed = _parse_json_field(job['meta']) if 'meta' in job.keys() else {}
    # Legacy graph stores structured script in a real `script` column;
    # canonical graph (no such column) stores it under meta['script'].
    script_parsed = (
        _parse_json_field(job['script']) if 'script' in job.keys()
        else (meta_parsed or {}).get('script', {})
    )
    status_c = canonicalize(job['status'])
    return {
        'job': job_out,
        'status': job['status'],
        'script': script_parsed,
        'meta': meta_parsed,
        'awaiting_approval': status_c == 'review',
        'approved': status_c in ('approved', 'published'),
        'assets': [_serialize_row(a) for a in assets],
        'events': [_serialize_row(e) for e in events],
        'status_history': status_history,
        'work': [_serialize_row(w) for w in work],
        'outbox': [_serialize_row(o) for o in outbox_rows],
    }


async def advance_job(job_id: UUID, to_status: str, payload: dict[str, Any]):
    # Audit F5: generic advance must not set approved/delivered.
    # approved → POST /jobs/{id}/approve (audit fields + optional distribute enqueue)
    # delivered → distribute worker only (pipeline.distribute)
    if to_status in ('approved', 'delivered'):
        raise ValueError(
            f'cannot advance to {to_status} via generic advance: '
            f'use POST /jobs/{{id}}/approve for approved, and the distribute '
            f'worker for delivered'
        )
    async with transaction() as conn:
        return await advance(conn, job_id, to_status, payload)


async def approve_job(
    job_id: UUID,
    *,
    approved_by: str | None = None,
    approved_by_key: str | None = None,
    enqueue_distribute: bool = True,
    note: str | None = None,
) -> dict:
    """Flip staged/review → approved. Optionally enqueue distribute stub (no public post).

    Identity of record for `approved_by` / `approved_by_key_id`:
      - `approved_by_key` (the raw credential a caller presented to pass
        HTTP auth — see app/api/jobs.py) is resolved against `api_keys`
        via verify_api_key(). A verified match wins: it is the only path
        that can set `approved_by_key_id` (a real FK, never spoofable).
        No match (no api_keys rows provisioned yet — today's common
        deployment) falls back to a label derived from the credential
        itself, never from arbitrary client text.
      - `approved_by` is a plain string honored only for *trusted direct
        callers* — the CLI (already has raw DATABASE_URL access, the same
        trust tier as SQL) and internal/test code calling this function
        directly in-process. The public HTTP endpoint no longer forwards
        its request body's `approved_by` field as the actor of record;
        see the note in app/api/jobs.py::approve_endpoint.
    """
    from . import queue

    async with transaction() as conn:
        job = await conn.fetchrow('SELECT * FROM jobs WHERE id=$1 FOR UPDATE', job_id)
        if not job:
            raise LookupError('job not found')
        # canonicalize() makes this check graph-agnostic: 'staged' (legacy)
        # and 'review' (canonical) both mean "awaiting human approval".
        # Also correctly refuses a second approve (status is already
        # 'approved', which canonicalizes to itself, not 'review').
        if canonicalize(job['status']) != 'review':
            raise ValueError(
                f'job status is {job["status"]}, expected staged/review '
                f'(nothing auto-publishes; approve only from staged/review)'
            )

        identity = await resolve_actor_identity(conn, approved_by_key)
        if identity is not None:
            who = identity['name']
            who_key_id = UUID(identity['id'])
            identity_source = 'api_keys'
        elif approved_by_key:
            who = unverified_identity_label(approved_by_key)
            who_key_id = None
            identity_source = 'shared_secret_unverified'
        else:
            who = (approved_by or 'operator').strip() or 'operator'
            who_key_id = None
            identity_source = 'trusted_caller' if approved_by else 'default'

        await conn.execute(
            """
            UPDATE jobs
            SET approved_at = now(),
                approved_by = $2,
                approved_by_key_id = $3,
                meta = COALESCE(meta, '{}'::jsonb) || $4::jsonb,
                updated_at = now()
            WHERE id = $1
            """,
            job_id,
            who,
            who_key_id,
            json.dumps({
                'awaiting_approval': False,
                'approval_note': note,
                'auto_publish': False,
                'approval_identity_source': identity_source,
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
                'approved_by': who,
                'note': 'Phase 4 distribute — YouTube live if creds; else staging-only',
            })
    return {
        'job': _serialize_row(row),
        'status': 'approved',
        'approved_by': who,
        'distribute_enqueued': work is not None,
        'work_id': int(work['id']) if work else None,
        'public_post': False,
        'note': (
            'Approved. Distribute runs via worker: YouTube when OAuth env set + local '
            'video; otherwise staging export only. TikTok/Reels never auto-post.'
        ),
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
