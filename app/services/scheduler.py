"""Daily cadence scheduler — create N jobs/day for the content pipeline.

Cron-friendly CLI + in-process admin tick. Does NOT render or publish;
only creates script_ready jobs (and optionally enqueues generate_avatar).
Nothing public-posts without the Phase 3 approve gate.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..config import settings
from ..db import transaction
from . import jobs, metrics, queue


def _tz():
    try:
        return ZoneInfo(settings.schedule_timezone or "America/Los_Angeles")
    except Exception:
        return ZoneInfo("America/Los_Angeles")


def today_local() -> date:
    return datetime.now(_tz()).date()


def topics_pool() -> list[str]:
    raw = (settings.schedule_topics or "").strip()
    if not raw:
        return [
            "morning hustle tips for founders",
            "one outreach line that books calls",
            "stop doomscrolling — ship today",
        ]
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                return [str(x) for x in data]
        except json.JSONDecodeError:
            pass
    return [t.strip() for t in raw.split(",") if t.strip()]


async def jobs_created_today(conn, day: date | None = None) -> int:
    day = day or today_local()
    row = await conn.fetchrow(
        "SELECT count(*)::int AS n FROM schedule_runs WHERE run_date = $1",
        day,
    )
    return int(row["n"] if row else 0)


async def tick(
    *,
    count: int | None = None,
    topics: list[str] | None = None,
    enqueue_avatar: bool | None = None,
    day: date | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Create up to ``count`` jobs for the local calendar day (cadence fill).

    Respects SCHEDULE_POSTS_PER_DAY unless force=True.
    """
    day = day or today_local()
    target = count if count is not None else int(settings.schedule_posts_per_day)
    target = max(0, min(target, 50))
    pool = topics if topics else topics_pool()
    do_avatar = (
        bool(settings.schedule_enqueue_avatar)
        if enqueue_avatar is None
        else bool(enqueue_avatar)
    )
    platforms = settings.schedule_platforms_list

    created: list[dict[str, Any]] = []
    skipped_reason = None

    async with transaction() as conn:
        already = await jobs_created_today(conn, day)
        remaining = target if force else max(0, int(settings.schedule_posts_per_day) - already)
        if count is not None and not force:
            remaining = min(remaining, target)
        elif count is not None and force:
            remaining = target
        if remaining <= 0:
            return {
                "run_date": day.isoformat(),
                "timezone": settings.schedule_timezone,
                "posts_per_day": int(settings.schedule_posts_per_day),
                "already_created_today": already,
                "created": [],
                "created_count": 0,
                "skipped": True,
                "reason": "daily cadence already met",
            }

        # Determine next slot numbers
        row = await conn.fetchrow(
            "SELECT COALESCE(max(slot), 0)::int AS m FROM schedule_runs WHERE run_date=$1",
            day,
        )
        next_slot = int(row["m"]) + 1

    for i in range(remaining):
        topic = pool[(already + i) % len(pool)]
        slot = next_slot + i
        row = await jobs.create_job(
            topic=topic,
            duration_target_seconds=int(settings.schedule_duration_seconds),
            platforms=platforms,
            auto_script_ready=True,
        )
        job_id = row["id"]
        work_id = None
        if do_avatar:
            work = await queue.enqueue_step(job_id, "generate_avatar", {"via": "scheduler"})
            work_id = int(work["id"])

        async with transaction() as conn:
            await conn.execute(
                """
                INSERT INTO schedule_runs(run_date, slot, job_id, topic, meta)
                VALUES($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (run_date, slot) DO NOTHING
                """,
                day,
                slot,
                job_id,
                topic,
                json.dumps({"enqueue_avatar": do_avatar, "work_id": work_id}),
            )

        await metrics.incr("jobs_created", labels={"source": "scheduler"})
        await metrics.incr("schedule_jobs_created")
        created.append({
            "job_id": str(job_id),
            "slot": slot,
            "topic": topic,
            "status": row["status"],
            "avatar_work_id": work_id,
        })

    return {
        "run_date": day.isoformat(),
        "timezone": settings.schedule_timezone,
        "posts_per_day": int(settings.schedule_posts_per_day),
        "already_created_today": already,
        "created": created,
        "created_count": len(created),
        "skipped": False,
        "enqueue_avatar": do_avatar,
        "note": (
            "Jobs created at script_ready. Render/distribute still require worker + "
            "explicit human approve before any public post."
        ),
    }


async def status() -> dict[str, Any]:
    day = today_local()
    async with transaction() as conn:
        n = await jobs_created_today(conn, day)
        rows = await conn.fetch(
            """
            SELECT slot, job_id, topic, created_at
            FROM schedule_runs WHERE run_date=$1 ORDER BY slot
            """,
            day,
        )
    return {
        "run_date": day.isoformat(),
        "timezone": settings.schedule_timezone,
        "posts_per_day": int(settings.schedule_posts_per_day),
        "created_today": n,
        "remaining": max(0, int(settings.schedule_posts_per_day) - n),
        "slots": [
            {
                "slot": r["slot"],
                "job_id": str(r["job_id"]) if r["job_id"] else None,
                "topic": r["topic"],
                "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"],
            }
            for r in rows
        ],
        "topics_pool": topics_pool(),
    }
