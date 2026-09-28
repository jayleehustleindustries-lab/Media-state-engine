"""Durable guards for Job Command campaign publishes (PR22-F2, PR21-F6/PR22-F6).

* Single-use approvals: a verified ``approval_token`` is spent exactly once in
  ``job_command_spent_approvals`` (PK = sha256(token), UNIQUE(campaign_id,
  approval_id)). A different event presenting the same approval is a replay
  and stays ``pending_approval`` (HTTP 409). The spend row is bound to the
  event's idempotency key, so a retry of the *same* event is still allowed.
* Publish idempotency: ``job_command_publish_log`` (PK = idempotency_key) is
  claimed before Pub/Sub publish. A retry of an already published event
  returns the prior ``message_id`` without republishing; a concurrent in-flight
  duplicate gets ``in_progress`` (HTTP 409).

Both writes happen in ONE transaction, so a replay also rolls back its publish
claim. Nothing here stores the bearer token itself.

Configuration: ``JOB_COMMAND_CAMPAIGN_DATABASE_URL`` only. There is deliberately
no fallback to the Media State Engine ``DATABASE_URL``. Unset => every publish
is refused (fail closed).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import Any, Callable

try:  # Support both package and direct script execution.
    from .job_command_pipeline_policy import (
        RENDER_EVENT, JobCommandEvent, approval_token_sha256, canonical_event_payload,
        idempotency_key, verify_render_approval,
    )
except ImportError:  # pragma: no cover - direct script entry point
    from job_command_pipeline_policy import (
        RENDER_EVENT, JobCommandEvent, approval_token_sha256, canonical_event_payload,
        idempotency_key, verify_render_approval,
    )

PENDING_STALE_SECONDS = 300


class CampaignStoreNotConfigured(RuntimeError):
    """No durable store => refuse to publish (fail closed)."""


class ApprovalReplay(RuntimeError):
    """The approval was already spent by a different event."""


class PublishInProgress(RuntimeError):
    """Another request is currently publishing this exact event."""


@dataclass
class PublishClaim:
    idempotency_key: str
    duplicate: bool
    message_id: str | None = None


class CampaignStore:
    def __init__(self, dsn: str):
        if not dsn:
            raise CampaignStoreNotConfigured("JOB_COMMAND_CAMPAIGN_DATABASE_URL is not configured.")
        self._dsn = dsn

    async def _connect(self):
        import asyncpg

        return await asyncpg.connect(self._dsn)

    async def begin_publish(self, event: JobCommandEvent, *, approval_claims: dict[str, Any] | None) -> PublishClaim:
        key = idempotency_key(event)
        conn = await self._connect()
        try:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """
                    INSERT INTO job_command_publish_log(idempotency_key, event_id, event_type, campaign_id)
                    VALUES($1, $2, $3, $4)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING idempotency_key
                    """,
                    key, event.event_id, event.event_type, event.campaign_id,
                )
                if not inserted:
                    row = await conn.fetchrow(
                        """
                        SELECT status, message_id,
                               updated_at < now() - make_interval(secs => $2) AS stale
                        FROM job_command_publish_log WHERE idempotency_key=$1 FOR UPDATE
                        """,
                        key, float(PENDING_STALE_SECONDS),
                    )
                    if row["status"] == "published":
                        return PublishClaim(key, duplicate=True, message_id=row["message_id"])
                    if row["status"] == "pending" and not row["stale"]:
                        raise PublishInProgress("This event is already being published; retry shortly.")
                    await conn.execute(
                        """
                        UPDATE job_command_publish_log
                        SET status='pending', attempts=attempts+1, last_error=NULL
                        WHERE idempotency_key=$1
                        """,
                        key,
                    )
                if event.event_type == RENDER_EVENT:
                    await self._spend_approval(conn, event, key, approval_claims or {})
            return PublishClaim(key, duplicate=False)
        finally:
            await conn.close()

    async def _spend_approval(self, conn, event: JobCommandEvent, key: str, claims: dict[str, Any]) -> None:
        token_hash = approval_token_sha256(event)
        if not token_hash or not event.approval_ref:
            raise ApprovalReplay("render approval is missing.")
        expires = claims.get("expires_at")
        expires_at = datetime.fromtimestamp(int(expires), tz=timezone.utc) if expires is not None else None
        spent = await conn.fetchval(
            """
            INSERT INTO job_command_spent_approvals(
              token_sha256, approval_id, campaign_id, idempotency_key, event_id, approved_by, expires_at)
            VALUES($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT DO NOTHING
            RETURNING token_sha256
            """,
            token_hash, event.approval_ref, event.campaign_id, key, event.event_id,
            str(claims.get("approved_by") or ""), expires_at,
        )
        if spent:
            return
        prior = await conn.fetchval(
            """
            SELECT idempotency_key FROM job_command_spent_approvals
            WHERE token_sha256=$1 OR (campaign_id=$2 AND approval_id=$3)
            LIMIT 1
            """,
            token_hash, event.campaign_id, event.approval_ref,
        )
        if prior != key:
            raise ApprovalReplay("This approval has already been used for a different render request.")

    async def complete_publish(self, key: str, message_id: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                """
                UPDATE job_command_publish_log
                SET status='published', message_id=$2, published_at=now(), last_error=NULL
                WHERE idempotency_key=$1
                """,
                key, message_id,
            )
        finally:
            await conn.close()

    async def abort_publish(self, key: str, error: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                "UPDATE job_command_publish_log SET status='failed', last_error=$2 WHERE idempotency_key=$1",
                key, error[:500],
            )
        finally:
            await conn.close()


def store_from_env() -> CampaignStore:
    return CampaignStore(os.getenv("JOB_COMMAND_CAMPAIGN_DATABASE_URL", "").strip())


PublishFn = Callable[[bytes, dict[str, str]], str]


async def guarded_publish(
    event: JobCommandEvent,
    *,
    publish_fn: PublishFn,
    attributes: dict[str, str],
    store: CampaignStore | None = None,
) -> dict[str, Any]:
    """Dedupe + single-use approval spend, then publish the sanitized payload.

    ``publish_fn(payload, attributes) -> message_id`` is blocking (Pub/Sub
    future) and runs in a worker thread. Raises CampaignStoreNotConfigured,
    ApprovalReplay, PublishInProgress, or the publish error.
    """
    store = store or store_from_env()
    claims = verify_render_approval(event) if event.event_type == RENDER_EVENT else None
    claim = await store.begin_publish(event, approval_claims=claims)
    if claim.duplicate:
        return {"published": True, "duplicate": True, "message_id": claim.message_id,
                "idempotency_key": claim.idempotency_key}
    try:
        message_id = await asyncio.to_thread(publish_fn, canonical_event_payload(event), attributes)
    except Exception as exc:
        await store.abort_publish(claim.idempotency_key, type(exc).__name__)
        raise
    await store.complete_publish(claim.idempotency_key, str(message_id))
    return {"published": True, "duplicate": False, "message_id": str(message_id),
            "idempotency_key": claim.idempotency_key}


__all__ = [
    "ApprovalReplay", "CampaignStore", "CampaignStoreNotConfigured", "PublishClaim",
    "PublishInProgress", "guarded_publish", "store_from_env",
]
