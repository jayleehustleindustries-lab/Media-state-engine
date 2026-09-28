"""PR21-F4: durable limits for ElevenLabs signed-URL minting (fail closed).

Every ``POST /voice/sessions`` reserves one attempt row in Postgres BEFORE the
provider is called. Reservations are serialized with a transaction-scoped
advisory lock so concurrent API instances cannot overshoot a cap. Attempts are
counted whether or not the provider call later succeeds: a failing or retried
provider call still costs budget, which is the conservative choice.

Limits (all env-configurable, see ``app/config.py``):
  * global UTC-day cap           JOB_COMMAND_VOICE_DAILY_MINT_CAP (default 50)
  * per caller API key / window  JOB_COMMAND_VOICE_RATE_LIMIT_PER_KEY
  * per client IP / window       JOB_COMMAND_VOICE_RATE_LIMIT_PER_IP
  * per visitor_ref / window     JOB_COMMAND_VOICE_RATE_LIMIT_PER_VISITOR
  * window length (seconds)      JOB_COMMAND_VOICE_RATE_WINDOW_SECONDS

Any limit configured <= 0 refuses every mint (fail closed), mirroring
``image_scorer_budget``.
"""
from __future__ import annotations

from hashlib import sha256
import logging
from typing import Any
from uuid import UUID

from ..config import settings

log = logging.getLogger("media_state.voice_mint_limits")

_LOCK_KEY = "job_command_voice_mint"
_RETENTION = "3 days"


class VoiceMintLimited(RuntimeError):
    """A mint was refused before any provider call (HTTP 429)."""

    def __init__(self, scope: str, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds


def fingerprint(value: str | None) -> str:
    """Stable non-reversible-at-a-glance identifier; raw keys/IPs are never stored."""
    raw = (value or "").strip()
    if not raw:
        return "unknown"
    return sha256(raw.encode("utf-8")).hexdigest()[:32]


def _limits() -> dict[str, int]:
    return {
        "daily": int(settings.job_command_voice_daily_mint_cap),
        "window": int(settings.job_command_voice_rate_window_seconds),
        "key": int(settings.job_command_voice_rate_limit_per_key),
        "ip": int(settings.job_command_voice_rate_limit_per_ip),
        "visitor": int(settings.job_command_voice_rate_limit_per_visitor),
    }


async def reserve_mint(
    conn,
    *,
    api_key_fingerprint: str,
    client_ip_fingerprint: str,
    visitor_ref: str,
) -> int:
    """Reserve one mint attempt or raise :class:`VoiceMintLimited`.

    Must run inside a transaction. Returns the reservation id.
    """
    limits = _limits()
    for name in ("daily", "window", "key", "ip", "visitor"):
        if limits[name] <= 0:
            raise VoiceMintLimited(
                name, f"Job Command voice minting is disabled by configuration ({name} limit <= 0)."
            )
    window = limits["window"]

    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", _LOCK_KEY)
    await conn.execute(
        f"DELETE FROM voice_mint_attempts WHERE created_at < now() - interval '{_RETENTION}'"
    )
    counts = await conn.fetchrow(
        """
        SELECT
          count(*) FILTER (WHERE day_utc = (now() AT TIME ZONE 'utc')::date) AS daily,
          count(*) FILTER (WHERE api_key_fingerprint = $1
                             AND created_at > now() - make_interval(secs => $4)) AS per_key,
          count(*) FILTER (WHERE client_ip_fingerprint = $2
                             AND created_at > now() - make_interval(secs => $4)) AS per_ip,
          count(*) FILTER (WHERE visitor_ref = $3
                             AND created_at > now() - make_interval(secs => $4)) AS per_visitor
        FROM voice_mint_attempts
        WHERE created_at > now() - interval '2 days'
        """,
        api_key_fingerprint,
        client_ip_fingerprint,
        visitor_ref,
        float(window),
    )
    if int(counts["daily"]) >= limits["daily"]:
        log.error("job command voice daily mint cap exhausted used=%s cap=%s", counts["daily"], limits["daily"])
        raise VoiceMintLimited("daily", "Job Command voice daily session cap reached; try again tomorrow (UTC).")
    if int(counts["per_key"]) >= limits["key"]:
        raise VoiceMintLimited("key", "Too many voice sessions for this API key; slow down.", window)
    if int(counts["per_ip"]) >= limits["ip"]:
        raise VoiceMintLimited("ip", "Too many voice sessions from this client; slow down.", window)
    if int(counts["per_visitor"]) >= limits["visitor"]:
        raise VoiceMintLimited("visitor", "Too many voice sessions for this visitor; slow down.", window)

    row = await conn.fetchrow(
        """
        INSERT INTO voice_mint_attempts(api_key_fingerprint, client_ip_fingerprint, visitor_ref)
        VALUES($1, $2, $3)
        RETURNING id
        """,
        api_key_fingerprint,
        client_ip_fingerprint,
        visitor_ref,
    )
    return int(row["id"])


async def record_outcome(conn, attempt_id: int, *, outcome: str, session_id: UUID | Any | None = None) -> None:
    if outcome not in {"minted", "failed"}:
        raise ValueError("outcome must be 'minted' or 'failed'")
    await conn.execute(
        "UPDATE voice_mint_attempts SET outcome=$2, session_id=COALESCE($3, session_id) WHERE id=$1",
        attempt_id,
        outcome,
        session_id,
    )


__all__ = ["VoiceMintLimited", "fingerprint", "reserve_mint", "record_outcome"]
