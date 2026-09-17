# Deploy

## Schema source of truth (CRITICAL)

Apply **in order**:

1. `supabase/migrations/20260317000001_media_state_engine_core.sql`
2. `supabase/migrations/20260317000002_media_state_engine_auth_and_quality.sql`
3. `supabase/migrations/20260317000003_app_adjuncts.sql`
4. `supabase/migrations/20260317000004_image_gate.sql`

```bash
export DATABASE_URL=postgresql://...
./scripts/apply_migrations.sh
```

Legacy `schema.sql` + `migrations/00x_phase*.sql` are **LEGACY** — do not apply to new environments.
Approval gate remains sacred: **publish only via `approved → published`**.
Image gate is mandatory before HeyGen: **`assert_image_pass_for_heygen` in same txn as key reserve**.


# Deploy — Media State Engine (FastAPI)

## Schema source of truth (critical)

**This service uses only:**

1. `schema.sql` (base + consolidated FastAPI tables)
2. `migrations/*.sql` in lexical order (`001` → `004` …)

```bash
psql "$DATABASE_URL" -f schema.sql
for f in migrations/*.sql; do psql "$DATABASE_URL" -f "$f"; done
```

On a fresh DB, `schema.sql` already includes Phase 1–4 objects; re-running
`migrations/*.sql` is idempotent (`IF NOT EXISTS` / additive ALTERs). Prefer
applying both so ops muscle memory stays consistent.

### Do NOT use the Drive / Supabase SQL packs

Files such as `001_media_state_engine_core.sql` and
`002_media_state_engine_auth_and_quality.sql` (under any `supabase-schema/`
tree) describe a **different** status vocabulary and RPC graph (`draft`,
`tts_*`, `published`, etc.). Applying them to this FastAPI database will
break workers and can reintroduce approval-skip edges that this app does
not use. **Never deploy those packs for this service.**

## Processes (same Docker image)

| Process | Command | Health |
|---------|---------|--------|
| API | `uvicorn app.main:app --host 0.0.0.0 --port 8080` | `GET /health` |
| Worker | `python -m app.worker` | no HTTP; relies on process restart |

Railway: default `railway.toml` starts the API with `/health`. Add a second
service from the same image with start command `python -m app.worker`
(disable HTTP healthcheck on the worker service).

## Required environment

### Both API and worker

- `DATABASE_URL` — Postgres with `pgcrypto`
- Provider keys as needed: `HEYGEN_*`, `ELEVENLABS_*`, optional Remotion

### API primarily

- `MEDIA_ENGINE_API_KEY` or `API_KEY` — **required**; middleware returns 503 if unset
- `PUBLIC_BASE_URL` — builds HeyGen callback URL
- Inbound webhook secrets: `HEYGEN_WEBHOOK_SECRET`, etc.
- Optional: `WEBHOOK_URL` / `WEBHOOK_SECRET` (review outbox)

### Worker primarily

- Same `DATABASE_URL` and provider credentials (HeyGen, ElevenLabs, YouTube OAuth)
- `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` / `YOUTUBE_REFRESH_TOKEN` for live Shorts; omit for staging-only
- `YOUTUBE_PRIVACY_STATUS` — default `private`
- `WORKER_*`, `STUCK_JOB_SECONDS`, `WORK_QUEUE_STALE_SECONDS`, `OUTBOX_STALE_SECONDS`

### Scheduler (API admin tick or CLI)

- `SCHEDULE_POSTS_PER_DAY`, `SCHEDULE_TIMEZONE`, `SCHEDULE_TOPICS`, …
- Keep `SCHEDULE_ENQUEUE_AVATAR=false` unless you intend to burn HeyGen from cron

## Approval / publish path

Jobs land in `staged` after render. Public/live distribute requires
`POST /jobs/{id}/approve` (not generic `/advance`). Worker runs
`distribute` after approve. Generic advance **rejects** `approved` and
`delivered` targets (audit F5).

## Checklist

1. Apply FastAPI `schema.sql` + `migrations/` only
2. Set API key (fail-closed)
3. Deploy API + worker from the same image
4. Confirm `GET /health` on the API
5. Cron `python -m app.cli schedule-tick` (or admin tick) as needed
6. Leave YouTube OAuth empty until ready; privacy stays `private` by default
