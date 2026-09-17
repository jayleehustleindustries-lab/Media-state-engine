# Media State Engine

PostgreSQL-backed state engine for short-form vertical video: script → HeyGen
avatar (optional ElevenLabs audio) → staged review → **human approve** →
YouTube Shorts distribute (or staging export). Illegal transitions and
duplicate paid provider calls are blocked at the DB/app layer.

## Guarantees

- Every status change locks the job (`SELECT … FOR UPDATE`), validates
  `app/state_machine.py` edges, writes an `events` row, then updates
  `jobs.status` in one transaction.
- Provider steps use `idempotency_keys` and a Postgres `work_queue`
  (`FOR UPDATE SKIP LOCKED`). Outbound review webhooks use
  `webhook_outbox` with backoff + DLQ.
- **Nothing public-posts without explicit approve.** Generic
  `POST /jobs/{id}/advance` cannot target `approved` or `delivered`
  (use `/approve` + distribute worker).
- API auth fails closed when `MEDIA_ENGINE_API_KEY` / `API_KEY` is unset.

## Status graph (current)

```
pending → script_ready → audio_generating → audio_ready
        ↘              ↘
         rendering → rendered → staged → approved → delivered
Any active state → failed. Terminal: delivered, failed.
```

`staged` = awaiting human approval. `approved → delivered` runs only via
the distribute worker after `/approve`.

## Local setup

Python 3.11+, Postgres with `pgcrypto`.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set DATABASE_URL + MEDIA_ENGINE_API_KEY
./scripts/apply_migrations.sh
uvicorn app.main:app --reload          # API (port 8000 local)
python -m app.worker                   # separate process
```

Docker / Railway use port **8080**. Health: `GET /health`.

> **Schema SoT for new deploys:** `supabase/migrations/` in lexical order,
> applied with `./scripts/apply_migrations.sh`. Root `schema.sql` +
> `migrations/00x_phase*.sql` are **LEGACY**. Details: [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Pipeline (operator)

1. `POST /jobs` — structured script + platform captions; usually lands
   `script_ready`.
2. `POST /jobs/{id}/generate-avatar` (and/or `generate-audio` / `render`) →
   **202** with `work_id`; worker performs provider I/O.
3. After render, job is **`staged`** (awaiting approval). Review via
   `GET /jobs/{id}/detail`.
4. `POST /jobs/{id}/approve` — sets audit fields, enqueues `distribute`.
5. Worker distribute: YouTube live when OAuth + local MP4 exist; otherwise
   staging under `data/staging/{job_id}/`. TikTok/Reels captions stay
   documented-only (never auto-posted).

Auth: `Authorization: Bearer $MEDIA_ENGINE_API_KEY` or `X-API-Key`.

```bash
curl -X POST "$HOST/jobs/$ID/approve" \
  -H "Authorization: Bearer $MEDIA_ENGINE_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"approved_by":"jordan"}'
```

CLI: `python -m app.cli approve <job_id> --by jordan`

## Scheduling + metrics (Phase 4)

```bash
python -m app.cli schedule-tick
python -m app.cli schedule-status
# or POST /admin/scheduler/tick  (API key)
```

Default cadence: `SCHEDULE_POSTS_PER_DAY` (often 2–3) in
`America/Los_Angeles`. Keep `SCHEDULE_ENQUEUE_AVATAR=false` unless you
intend cron to enqueue HeyGen. Metrics: `GET /metrics` /
`GET /admin/metrics`.

## Configuration (high level)

| Area | Vars |
|------|------|
| DB / auth | `DATABASE_URL`, `MEDIA_ENGINE_API_KEY` |
| HeyGen | `HEYGEN_API_KEY`, avatar/voice ids, webhook secret, `PUBLIC_BASE_URL` |
| ElevenLabs | `ELEVENLABS_*` (optional path) |
| Outbox | `WEBHOOK_URL`, `WEBHOOK_SECRET` |
| Worker | `WORKER_*`, `STUCK_JOB_SECONDS`, stale reclaim seconds |
| Schedule | `SCHEDULE_*` |
| YouTube | `YOUTUBE_CLIENT_*`, `YOUTUBE_REFRESH_TOKEN`, `YOUTUBE_PRIVACY_STATUS` |

See `.env.example` and [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Deployment

Same image for API and worker — see Dockerfile comments and
[`docs/DEPLOY.md`](docs/DEPLOY.md). Railway healthcheck: `/health`.

## Tests

```bash
pytest -q
# Postgres integration tests skip when DATABASE_URL is unset
```

## Phase reports

| Phase | Report |
|-------|--------|
| 1 Auth, outbox/DLQ, reconcile, asset kinds | [PHASE1_REPORT.md](PHASE1_REPORT.md) |
| 2 Work queue worker + async providers | [PHASE2_REPORT.md](PHASE2_REPORT.md) |
| 3 Script quality, dual-format guard, approve gate | [PHASE3_REPORT.md](PHASE3_REPORT.md) |
| 4 Scheduler, YouTube distribute, metrics | [PHASE4_REPORT.md](PHASE4_REPORT.md) |
| 5 Deploy docs, SoT, audit F1–F5 fixes | [PHASE5_REPORT.md](PHASE5_REPORT.md) |
