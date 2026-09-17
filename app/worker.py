"""Background worker for provider I/O and outbox flush.

Run:
    python -m app.worker

Uses the Postgres ``work_queue`` table (FOR UPDATE SKIP LOCKED). No Redis.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

from .config import settings
from .db import connect, close, transaction
from .services import queue, pipeline, outbox

log = logging.getLogger("media_state.worker")

_stop = asyncio.Event()


def _install_signals() -> None:
    loop = asyncio.get_running_loop()

    def _handler(*_args):
        log.info("shutdown signal received")
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _handler())


async def process_work_item(item: dict[str, Any]) -> None:
    """Execute one claimed work_queue row and finalize status."""
    work_id = int(item["id"])
    job_id = item["job_id"]
    step = item["step"]
    attempts = int(item["attempts"])
    max_attempts = int(item["max_attempts"])
    payload = item.get("payload") or {}
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)

    log.info("processing work_id=%s job_id=%s step=%s attempt=%s", work_id, job_id, step, attempts)
    try:
        result: Any
        if step == "score_image":
            from .services.image_gate import run_score_image_step
            async with transaction() as conn:
                result = await run_score_image_step(
                    conn,
                    job_id,
                    candidate_url=payload.get("candidate_url") or "",
                    prompt=payload.get("prompt") or "",
                )
        elif step == "revise_image":
            from .services.image_gate import run_revise_image_step
            async def _regen(jid, prompt, refs):
                return {
                    "url": payload.get("revised_url")
                    or payload.get("candidate_url")
                    or "mock://revised"
                }
            async with transaction() as conn:
                result = await run_revise_image_step(conn, job_id, regenerate=_regen)
        elif step == "generate_audio":
            result = await pipeline.generate_audio(job_id)
        elif step == "generate_avatar":
            result = await pipeline.generate_avatar(
                job_id,
                avatar_id=payload.get("avatar_id"),
                voice_id=payload.get("voice_id"),
                include_horizontal=payload.get("include_horizontal"),
            )
        elif step == "render":
            result = await pipeline.render(job_id)
        elif step == "reconcile":
            result = await pipeline.reconcile_job(job_id)
        elif step == "flush_outbox":
            result = await outbox.flush_outbox(limit=int(payload.get("limit", 50)))
        elif step == "distribute":
            # Phase 4 — refuses unless approved; YouTube live only with OAuth + local file
            result = await pipeline.distribute(job_id)
        else:
            raise ValueError(f"unknown step: {step}")

        async with transaction() as conn:
            await queue.mark_done(conn, work_id, result if isinstance(result, dict) else {"ok": True})
        log.info("work_id=%s done", work_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("work_id=%s failed: %s", work_id, exc)
        async with transaction() as conn:
            status = await queue.mark_failure(conn, work_id, str(exc), attempts, max_attempts)
        log.warning("work_id=%s marked %s", work_id, status)


async def tick(*, also_reconcile: bool = False) -> dict[str, int]:
    """One worker cycle: reclaim stale claims, claim work, process, flush outbox."""
    stats: dict = {"claimed": 0, "processed": 0, "outbox": {}, "reclaimed_work": 0, "reclaimed_outbox": 0}
    # Audit F4: always reclaim crashed running/delivering before claiming
    async with transaction() as conn:
        rw = await queue.reclaim_stale_running(conn)
        ro = await outbox.reclaim_stale_delivering(conn)
        stats["reclaimed_work"] = len(rw)
        stats["reclaimed_outbox"] = len(ro)
        items = await queue.claim_due(conn, limit=settings.worker_batch_size)
    stats["claimed"] = len(items)
    for item in items:
        await process_work_item(item)
        stats["processed"] += 1

    stats["outbox"] = await outbox.flush_outbox(limit=20)

    if also_reconcile:
        try:
            recon = await pipeline.reconcile_stuck(limit=10)
            stats["reconcile"] = {"checked": recon.get("checked", 0)}
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile_stuck failed: %s", exc)
            stats["reconcile"] = {"error": str(exc)}
    return stats


async def run_forever() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await connect()
    _install_signals()
    log.info(
        "worker started poll=%.1fs batch=%s db=%s",
        settings.worker_poll_interval_seconds,
        settings.worker_batch_size,
        settings.database_url.split("@")[-1] if "@" in settings.database_url else "(set)",
    )
    last_reconcile = 0.0
    try:
        while not _stop.is_set():
            now = asyncio.get_running_loop().time()
            do_recon = (now - last_reconcile) >= settings.worker_reconcile_interval_seconds
            try:
                stats = await tick(also_reconcile=do_recon)
                if do_recon:
                    last_reconcile = now
                if stats["claimed"] == 0 and not stats["outbox"].get("claimed"):
                    try:
                        await asyncio.wait_for(
                            _stop.wait(),
                            timeout=settings.worker_poll_interval_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
                else:
                    # Busy: yield briefly then continue
                    await asyncio.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                log.exception("worker tick error: %s", exc)
                await asyncio.sleep(settings.worker_poll_interval_seconds)
    finally:
        await close()
        log.info("worker stopped")


def main() -> None:
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
