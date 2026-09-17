# Phase 2 Report — Background Worker + Pipeline

**Branch:** `feat/phase1-harden-core` (Phases 1–2 on one feature branch)  
**Date:** 2026-09-17 (PT)  
**Scope:** Move provider I/O off the API into a durable background worker; HeyGen callback_url; job detail; happy-path delivery.

## Queue / worker choice

**Postgres `work_queue` (custom, FOR UPDATE SKIP LOCKED)** — not RQ or Celery.

| Option | Why not / why yes |
|--------|-------------------|
| RQ | Needs Redis; not in stack |
| Celery | Needs Redis/RabbitMQ; heavy for this service |
| **Postgres work_queue** | Already have Postgres + the same SKIP LOCKED pattern as `webhook_outbox`. Zero new infrastructure. |

### How to run the worker

```bash
export DATABASE_URL=postgresql://...
# optional: PUBLIC_BASE_URL=https://your-api.example.com
python -m app.worker
```

Ops alternative (no separate process): `POST /admin/worker/tick` (auth required).

Worker loop: claim due `work_queue` rows → run step → mark done/retry/dead → flush outbox → periodic `reconcile_stuck`.

## HeyGen path choice

**Primary: Direct Video (`POST /v3/videos`)** — not Video Agent.

- Matches existing Phase 1 pipeline and gives scripted control over avatar, voice, and dimensions.
- Video Agent is faster for free-form prompts but is the wrong fit for this batch/scripted state engine.
- Completion via **`callback_url`** (plus `callback_id`); Phase 1 **reconcile** remains the safety net for silent/missed webhooks.
- **`Idempotency-Key`** still sent on every paid create.

`callback_url` resolution: `HEYGEN_CALLBACK_URL` or `PUBLIC_BASE_URL` + `/webhooks/heygen`.

## Happy-path flow

```
pending → script_ready
       → [optional] generate-audio (202 → worker → audio_ready)
       → generate-avatar (202 → worker → HeyGen Direct Video → rendering + kind=video)
       → HeyGen webhook/callback (or reconcile) → rendered + kind=final + outbox enqueue
       → outbox delivery → delivered (when AUTO_DELIVER_ON_OUTBOX_SUCCESS=true)
```

Audio is optional: `script_ready` may go straight to `rendering` via generate-avatar.

## New / changed endpoints

| Method | Path | Change |
|--------|------|--------|
| POST | `/jobs/{id}/generate-audio` | **202** enqueue (`generate_audio`) |
| POST | `/jobs/{id}/generate-avatar` | **202** enqueue (`generate_avatar`) |
| POST | `/jobs/{id}/render` | **202** enqueue (`render`) |
| GET | `/jobs/{id}/detail` | **New** — assets, events, status_history, work, outbox |
| GET | `/work/{work_id}` | **New** — work item status |
| POST | `/admin/worker/tick` | **New** — one in-process worker cycle |
| POST | `/jobs/{id}/reconcile` | Unchanged (sync ops path) |
| POST | `/webhooks/heygen` | Unchanged (HMAC + complete) |

Phase 1 auth, outbox/DLQ, asset kinds (`video`→`final`), fail-on-provider-error: **intact**.

## Files changed / added

| Path | Action |
|------|--------|
| `app/services/queue.py` | **New** — enqueue/claim/done/fail |
| `app/worker.py` | **New** — background worker CLI |
| `app/api/jobs.py` | 202 enqueue, detail, work, worker tick |
| `app/services/jobs.py` | `get_job_detail` |
| `app/services/heygen.py` | `callback_url` on create |
| `app/services/outbox.py` | auto `rendered→delivered` after success |
| `app/config.py` | worker + public_base_url + heygen_callback_url |
| `schema.sql` / `migrations/002_phase2_work_queue.sql` | `work_queue` table |
| `.env.example` | Phase 2 env vars |
| `tests/test_phase2_queue.py` | **New** |
| `PHASE2_REPORT.md` | This report |
| `README.md` | Phase 2 section |

## Env vars added

```
PUBLIC_BASE_URL=
HEYGEN_CALLBACK_URL=
WORKER_POLL_INTERVAL_SECONDS=2.0
WORKER_BATCH_SIZE=5
WORKER_MAX_ATTEMPTS=5
WORKER_RECONCILE_INTERVAL_SECONDS=60.0
AUTO_DELIVER_ON_OUTBOX_SUCCESS=true
```

Existing: `DATABASE_URL`, `MEDIA_ENGINE_API_KEY` / `API_KEY`, `HEYGEN_API_KEY`, webhook secrets, etc. **No secrets committed.**

## Test results

```
30+ passed (full suite including Phase 1 + Phase 2)
```

Phase 2 coverage: enqueue dedupe, claim/done, retry→dead, worker tick + mocked HeyGen, job detail, API 202 (mocked enqueue), outbox→delivered, callback_url + Idempotency-Key on create.

## Stress-test decisions

| Risk | Mitigation |
|------|------------|
| **Stuck jobs** | Worker periodic `reconcile_stuck`; `POST /jobs/{id}/reconcile`; TTL on unfinished idempotency keys |
| **Paid burns** | API auth fail-closed; Idempotency-Key on HeyGen; one active work row per (job, step); keys cleared on fail |
| **Silent webhooks** | `callback_url` push + Phase 1 reconcile poll; do not rely on webhooks alone |
| **Hollow assets** | In-progress = `kind=video`; terminal = `kind=final` only after webhook/reconcile success |
| **Missing auth** | Middleware 503 if key unset; 401 if wrong; webhooks use HMAC only |

## Blockers / environment

- **No live HeyGen / ElevenLabs / Remotion keys** in this box — provider I/O covered by mocks.
- **PUBLIC_BASE_URL / HEYGEN_CALLBACK_URL** must be set in deploy for real callbacks.
- **Push/PR** not required for this phase; commit on feature branch only.
- Secrets never committed; `.env` gitignored.

## Ready for Phase 3

- Worker + queue durable; API non-blocking.
- Happy path wired through outbox → delivered.
- Next (Phase 3+): persist HeyGen MP4 bytes, dead-outbox replay tooling, metrics/alerts, optional Remotion/ElevenLabs live E2E.

## Success criteria checklist

- [x] Queue/worker choice justified + how to run
- [x] Happy-path flow confirmed
- [x] New/changed endpoints listed
- [x] Test results recorded
- [x] Commit on feature branch
- [x] Honest blockers above
- [x] Phase 3–5 **not** implemented
