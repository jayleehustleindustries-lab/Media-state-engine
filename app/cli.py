"""CLI for Media State Engine operators.

Examples:
  python -m app.cli approve <job_id>
  python -m app.cli approve <job_id> --by jordan --no-distribute
  python -m app.cli show <job_id>
  python -m app.cli script --topic "morning hustle tips" --duration 30
  python -m app.cli schedule-tick
  python -m app.cli schedule-status
  python -m app.cli metrics
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from uuid import UUID

from .db import connect, close
from .services import jobs, scriptgen, scheduler, metrics


async def cmd_approve(job_id: str, approved_by: str | None, enqueue_distribute: bool, note: str | None) -> int:
    await connect()
    try:
        result = await jobs.approve_job(
            UUID(job_id),
            approved_by=approved_by,
            enqueue_distribute=enqueue_distribute,
            note=note,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    finally:
        await close()


async def cmd_show(job_id: str) -> int:
    await connect()
    try:
        data = await jobs.get_job_detail(UUID(job_id))
        if not data:
            print('error: job not found', file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2, default=str))
        return 0
    finally:
        await close()


def cmd_script(args: argparse.Namespace) -> int:
    try:
        full, script, meta = scriptgen.compose_script(
            script_text=args.text,
            topic=args.topic,
            duration_target_seconds=args.duration,
            cta=args.cta,
            include_horizontal=args.horizontal,
            heygen_dual_paid=False,
        )
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    print(json.dumps({'script_text': full, 'script': script, 'meta': meta}, indent=2))
    return 0


async def cmd_schedule_tick(args: argparse.Namespace) -> int:
    await connect()
    try:
        topics = [t.strip() for t in (args.topics or '').split(',') if t.strip()] or None
        result = await scheduler.tick(
            count=args.count,
            topics=topics,
            enqueue_avatar=args.enqueue_avatar if args.enqueue_avatar else None,
            force=args.force,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    finally:
        await close()


async def cmd_schedule_status() -> int:
    await connect()
    try:
        print(json.dumps(await scheduler.status(), indent=2, default=str))
        return 0
    finally:
        await close()


async def cmd_metrics() -> int:
    await connect()
    try:
        print(json.dumps(await metrics.snapshot(), indent=2, default=str))
        return 0
    finally:
        await close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m app.cli', description='Media State Engine CLI')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_approve = sub.add_parser('approve', help='Approve a staged job (human gate before distribute)')
    p_approve.add_argument('job_id')
    p_approve.add_argument('--by', dest='approved_by', default='cli-operator')
    p_approve.add_argument('--note', default=None)
    p_approve.add_argument(
        '--no-distribute',
        action='store_true',
        help='Approve only; do not enqueue distribute',
    )

    p_show = sub.add_parser('show', help='Show job detail including script + captions')
    p_show.add_argument('job_id')

    p_script = sub.add_parser('script', help='Preview structured script generation (no DB)')
    p_script.add_argument('--topic', default=None)
    p_script.add_argument('--text', default=None)
    p_script.add_argument('--duration', type=int, default=30)
    p_script.add_argument('--cta', default=None)
    p_script.add_argument('--horizontal', action='store_true')

    p_tick = sub.add_parser(
        'schedule-tick',
        help='Create daily-cadence jobs (cron-friendly). Does not public-post.',
    )
    p_tick.add_argument('--count', type=int, default=None, help='Jobs to create this tick')
    p_tick.add_argument('--topics', default=None, help='Comma-separated topics override')
    p_tick.add_argument(
        '--enqueue-avatar',
        action='store_true',
        help='Also enqueue generate_avatar (burns HeyGen when worker runs)',
    )
    p_tick.add_argument(
        '--force',
        action='store_true',
        help='Ignore daily posts-per-day cap for this tick',
    )

    sub.add_parser('schedule-status', help='Show today cadence fill vs posts-per-day')
    sub.add_parser('metrics', help='Print pipeline metrics snapshot')

    args = parser.parse_args(argv)
    if args.cmd == 'approve':
        return asyncio.run(cmd_approve(
            args.job_id,
            args.approved_by,
            enqueue_distribute=not args.no_distribute,
            note=args.note,
        ))
    if args.cmd == 'show':
        return asyncio.run(cmd_show(args.job_id))
    if args.cmd == 'script':
        return cmd_script(args)
    if args.cmd == 'schedule-tick':
        return asyncio.run(cmd_schedule_tick(args))
    if args.cmd == 'schedule-status':
        return asyncio.run(cmd_schedule_status())
    if args.cmd == 'metrics':
        return asyncio.run(cmd_metrics())
    parser.error(f'unknown command {args.cmd}')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
