"""Durable outbound webhook outbox with exponential backoff and dead-letter."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from typing import Any
from uuid import UUID

import httpx

from ..config import settings


async def enqueue(
    conn,
    job_id: UUID,
    payload: dict[str, Any],
    *,
    destination_url: str | None = None,
    max_attempts: int | None = None,
) -> dict | None:
    """Insert an outbox row in the caller's transaction. Returns None if no destination."""
    url = destination_url or settings.webhook_url
    if not url:
        return None
    max_attempts = max_attempts if max_attempts is not None else settings.webhook_outbox_max_attempts
    row = await conn.fetchrow(
        """
        INSERT INTO webhook_outbox(job_id, destination_url, payload, max_attempts)
        VALUES($1, $2, $3::jsonb, $4)
        RETURNING *
        """,
        job_id,
        url,
        json.dumps(payload),
        max_attempts,
    )
    return dict(row) if row else None


def _backoff_seconds(attempts: int) -> float:
    base = settings.webhook_outbox_base_delay_seconds
    # attempts is post-increment count for next delay: 2^0, 2^1, ... capped
    return min(base * (2 ** max(attempts - 1, 0)), 3600.0)


def _sign(body: bytes) -> str:
    secret = settings.webhook_secret or ""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _deliver_once(destination_url: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "content-type": "application/json",
        "x-webhook-signature": _sign(body),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(destination_url, content=body, headers=headers)
        response.raise_for_status()


async def claim_due(conn, limit: int = 20) -> list:
    """Lock and mark due pending rows as delivering; return claimed rows."""
    rows = await conn.fetch(
        """
        WITH due AS (
          SELECT id FROM webhook_outbox
          WHERE status = 'pending' AND next_attempt_at <= now()
          ORDER BY next_attempt_at
          FOR UPDATE SKIP LOCKED
          LIMIT $1
        )
        UPDATE webhook_outbox o
        SET status = 'delivering', updated_at = now()
        FROM due WHERE o.id = due.id
        RETURNING o.*
        """,
        limit,
    )
    return list(rows)


async def mark_delivered(conn, outbox_id: int) -> None:
    await conn.execute(
        """
        UPDATE webhook_outbox
        SET status = 'delivered', delivered_at = now(), updated_at = now(), last_error = NULL
        WHERE id = $1
        """,
        outbox_id,
    )


async def mark_failure(conn, outbox_id: int, error: str, attempts: int, max_attempts: int) -> str:
    """Schedule retry or dead-letter. Returns new status."""
    if attempts >= max_attempts:
        await conn.execute(
            """
            UPDATE webhook_outbox
            SET status = 'dead', attempts = $2, last_error = $3, updated_at = now()
            WHERE id = $1
            """,
            outbox_id,
            attempts,
            error[:2000],
        )
        return "dead"
    delay = _backoff_seconds(attempts)
    await conn.execute(
        """
        UPDATE webhook_outbox
        SET status = 'pending',
            attempts = $2,
            last_error = $3,
            next_attempt_at = now() + ($4 || ' seconds')::interval,
            updated_at = now()
        WHERE id = $1
        """,
        outbox_id,
        attempts,
        error[:2000],
        str(delay),
    )
    return "pending"


async def process_due(conn, *, limit: int = 20, deliver=_deliver_once) -> dict[str, int]:
    """Claim due rows and attempt delivery. Caller must hold a connection/transaction context carefully.

    Each delivery is attempted outside a long-held lock: we claim in a short
    transaction, deliver, then finalize in a new transaction via the same conn
    if the caller uses autocommit-style acquires. Here we finalize in-place
    after each attempt within the same connection.
    """
    claimed = await claim_due(conn, limit=limit)
    stats = {"claimed": len(claimed), "delivered": 0, "retried": 0, "dead": 0}
    for row in claimed:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        attempts = int(row["attempts"]) + 1
        try:
            await deliver(row["destination_url"], dict(payload))
            await mark_delivered(conn, row["id"])
            stats["delivered"] += 1
        except Exception as exc:  # noqa: BLE001 — outbox must never crash the worker
            status = await mark_failure(conn, row["id"], str(exc), attempts, int(row["max_attempts"]))
            if status == "dead":
                stats["dead"] += 1
            else:
                stats["retried"] += 1
    return stats


async def flush_outbox(limit: int = 20) -> dict[str, int]:
    """Process due outbox rows using a fresh transaction."""
    from ..db import transaction

    async with transaction() as conn:
        return await process_due(conn, limit=limit)
