"""Outbound delivery helpers.

Durable path: enqueue into webhook_outbox inside the same DB transaction as the
state commit, then flush asynchronously. Fire-and-forget HTTP after commit is
kept only as a best-effort kick; the outbox is the source of truth.
"""
from __future__ import annotations

from uuid import UUID

from . import outbox
from ..db import transaction
from ..config import settings


async def delivery(job_id: UUID, payload: dict):
    """Enqueue (if not already done in-txn) and attempt an immediate flush.

    Prefer ``outbox.enqueue`` inside the committing transaction. This helper
    exists for call sites that already committed; it still enqueues before any
    HTTP attempt so a crash cannot silently drop the notification.
    """
    if not settings.webhook_url:
        return None
    async with transaction() as conn:
        await outbox.enqueue(conn, job_id, {'job_id': str(job_id), **payload})
    # Best-effort immediate delivery; failures stay in outbox for retry.
    return await outbox.flush_outbox(limit=10)


async def enqueue_in_txn(conn, job_id: UUID, payload: dict):
    return await outbox.enqueue(conn, job_id, {'job_id': str(job_id), **payload})
