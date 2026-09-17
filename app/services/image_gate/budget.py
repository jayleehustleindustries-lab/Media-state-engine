"""Durable UTC-day scorer budget (Postgres, redis-less). Fail closed when exhausted."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...config import settings

log = logging.getLogger("media_state.image_scorer_budget")


class BudgetExhausted(RuntimeError):
    """Daily scorer budget exhausted — do not call scorer or HeyGen."""


def _utc_day():
    return datetime.now(timezone.utc).date()


async def consume_scorer_budget(conn) -> int:
    """Atomically consume 1 unit of today's scorer budget.

    Returns remaining-used count after consume.
    Raises BudgetExhausted when the daily limit is already reached.
    """
    limit = int(settings.image_scorer_daily_budget)
    if limit <= 0:
        raise BudgetExhausted(
            f"image_scorer_daily_budget={limit} — scorer refused (fail closed)"
        )
    day = _utc_day()
    # Ensure row exists for today (do not increment yet)
    await conn.execute(
        """
        INSERT INTO image_scorer_budget (day_utc, used, budget_limit)
        VALUES ($1::date, 0, $2)
        ON CONFLICT (day_utc) DO UPDATE
          SET budget_limit = EXCLUDED.budget_limit,
              updated_at = now()
        """,
        day,
        limit,
    )
    row = await conn.fetchrow(
        """
        UPDATE image_scorer_budget
           SET used = used + 1,
               updated_at = now()
         WHERE day_utc = $1::date
           AND used < budget_limit
        RETURNING used, budget_limit
        """,
        day,
    )
    if not row:
        cur = await conn.fetchrow(
            "SELECT used, budget_limit FROM image_scorer_budget WHERE day_utc=$1::date",
            day,
        )
        used = int(cur["used"]) if cur else 0
        bl = int(cur["budget_limit"]) if cur else limit
        log.error("scorer daily budget exhausted day=%s used=%s limit=%s", day, used, bl)
        raise BudgetExhausted(
            f"daily scorer budget exhausted ({used}/{bl} UTC {day}) — refuse score"
        )
    log.debug(
        "scorer budget consumed day=%s used=%s/%s",
        day,
        row["used"],
        row["budget_limit"],
    )
    return int(row["used"])


async def ensure_budget_table(conn) -> None:
    """Best-effort create for test DBs that have not applied migration 005."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS image_scorer_budget (
          day_utc date PRIMARY KEY,
          used integer NOT NULL DEFAULT 0 CHECK (used >= 0),
          budget_limit integer NOT NULL CHECK (budget_limit >= 0),
          updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS image_gate_break_glass_audit (
          id bigserial PRIMARY KEY,
          job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          actor text NOT NULL DEFAULT 'system',
          reason text,
          env_flag boolean NOT NULL DEFAULT true,
          job_flag boolean NOT NULL DEFAULT true,
          meta jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
