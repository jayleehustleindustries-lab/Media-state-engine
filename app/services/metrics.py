"""Basic pipeline metrics — counters, latency samples, cost estimates."""
from __future__ import annotations

import json
from typing import Any

from ..db import transaction

# Default USD estimates (operator-overridable via env later)
DEFAULT_COST_USD = {
    "heygen_primary": 0.50,       # one Direct Video credit estimate
    "heygen_horizontal": 0.50,    # second paid dual call
    "elevenlabs_audio": 0.05,
    "remotion_render": 0.02,
    "youtube_upload": 0.0,
}


async def incr(name: str, by: int = 1, *, labels: dict[str, Any] | None = None) -> None:
    labels = labels or {}
    async with transaction() as conn:
        await conn.execute(
            """
            INSERT INTO metric_counters(name, value, labels, updated_at)
            VALUES($1, $2, $3::jsonb, now())
            ON CONFLICT (name) DO UPDATE
              SET value = metric_counters.value + EXCLUDED.value,
                  labels = metric_counters.labels || EXCLUDED.labels,
                  updated_at = now()
            """,
            name,
            by,
            json.dumps(labels),
        )


async def observe(name: str, value: float, *, labels: dict[str, Any] | None = None) -> None:
    labels = labels or {}
    async with transaction() as conn:
        await conn.execute(
            """
            INSERT INTO metric_samples(name, value, labels)
            VALUES($1, $2, $3::jsonb)
            """,
            name,
            float(value),
            json.dumps(labels),
        )


async def record_job_cost(job_id, estimate: dict[str, Any]) -> None:
    """Merge cost_estimate into jobs.meta (non-secret)."""
    async with transaction() as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET meta = jsonb_set(
              COALESCE(meta, '{}'::jsonb),
              '{cost_estimate}',
              $2::jsonb,
              true
            ),
            updated_at = now()
            WHERE id = $1
            """,
            job_id,
            json.dumps(estimate),
        )


def estimate_cost(*, heygen_paid_calls: int = 1, used_elevenlabs: bool = False,
                  used_remotion: bool = False) -> dict[str, Any]:
    items = []
    total = 0.0
    if heygen_paid_calls > 0:
        c = DEFAULT_COST_USD["heygen_primary"] * heygen_paid_calls
        items.append({"provider": "heygen", "calls": heygen_paid_calls, "usd": c})
        total += c
    if used_elevenlabs:
        c = DEFAULT_COST_USD["elevenlabs_audio"]
        items.append({"provider": "elevenlabs", "usd": c})
        total += c
    if used_remotion:
        c = DEFAULT_COST_USD["remotion_render"]
        items.append({"provider": "remotion", "usd": c})
        total += c
    return {
        "currency": "USD",
        "total_usd_estimate": round(total, 4),
        "items": items,
        "note": "Rough operator estimate — not billed amounts",
    }


async def snapshot() -> dict[str, Any]:
    async with transaction() as conn:
        counters = await conn.fetch(
            "SELECT name, value, labels, updated_at FROM metric_counters ORDER BY name"
        )
        # Recent latency samples (last 50 per known name family)
        samples = await conn.fetch(
            """
            SELECT name, value, labels, recorded_at
            FROM metric_samples
            ORDER BY recorded_at DESC
            LIMIT 100
            """
        )
        # Status histogram from jobs
        status_rows = await conn.fetch(
            "SELECT status, count(*)::bigint AS n FROM jobs GROUP BY status ORDER BY status"
        )
        # Avg cost estimate from recent delivered/staged jobs
        cost_rows = await conn.fetch(
            """
            SELECT id, meta->'cost_estimate' AS cost_estimate, status
            FROM jobs
            WHERE meta ? 'cost_estimate'
            ORDER BY updated_at DESC
            LIMIT 20
            """
        )
    def _ser(row):
        d = dict(row)
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif k in ("id",) and v is not None:
                d[k] = str(v)
            elif k in ("labels", "cost_estimate") and isinstance(v, str):
                d[k] = json.loads(v)
        return d

    counters_map = {r["name"]: {"value": int(r["value"]), "labels": r["labels"], "updated_at": r["updated_at"].isoformat() if hasattr(r["updated_at"], "isoformat") else r["updated_at"]} for r in counters}
    latencies: dict[str, list] = {}
    for s in samples:
        latencies.setdefault(s["name"], []).append({
            "value": float(s["value"]),
            "labels": s["labels"] if not isinstance(s["labels"], str) else json.loads(s["labels"]),
            "recorded_at": s["recorded_at"].isoformat() if hasattr(s["recorded_at"], "isoformat") else s["recorded_at"],
        })

    return {
        "counters": counters_map,
        "jobs_by_status": {r["status"]: int(r["n"]) for r in status_rows},
        "provider_latency_ms": latencies,
        "recent_cost_estimates": [_ser(r) for r in cost_rows],
        "fields": {
            "counters": [
                "jobs_created", "jobs_rendered", "jobs_staged", "jobs_delivered",
                "jobs_failed", "jobs_distributed_live", "jobs_distributed_staging",
                "schedule_jobs_created",
            ],
            "latency": ["provider_latency_ms.heygen", "provider_latency_ms.youtube", "provider_latency_ms.elevenlabs"],
            "cost": "jobs.meta.cost_estimate.total_usd_estimate",
        },
    }
