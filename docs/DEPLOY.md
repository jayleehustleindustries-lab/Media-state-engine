# Deploy — Media State Engine (FastAPI)

## Schema source of truth (critical)

For new environments, the single schema source of truth is `supabase/migrations/`,
applied in lexical order by the repository script:

1. `supabase/migrations/20260317000001_media_state_engine_core.sql`
2. `supabase/migrations/20260317000002_media_state_engine_auth_and_quality.sql`
3. `supabase/migrations/20260317000003_app_adjuncts.sql`
4. `supabase/migrations/20260317000004_image_gate.sql`
5. `supabase/migrations/20260317000005_image_gate_caps.sql`

```bash
export DATABASE_URL=postgresql://...
./scripts/apply_migrations.sh
```

The root `schema.sql` and `migrations/00x_phase*.sql` files are **LEGACY**
reference files; do not apply them to new environments.
Approval remains sacred: **publish only via `approved → published`**. The image
gate is mandatory before HeyGen: `assert_image_pass_for_heygen` runs in the
same transaction as key reservation.

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

Jobs land in `staged` after render. Public/live distribution requires
`POST /jobs/{id}/approve` (not generic `/advance`). The worker runs
`distribute` after approval. Generic advance **rejects** `approved` and
`delivered` targets (audit F5). There is no automatic public publishing.

## Checklist

1. Apply `supabase/migrations/` with `./scripts/apply_migrations.sh`
2. Set API key (fail-closed)
3. Deploy API + worker from the same image
4. Confirm `GET /health` on the API
5. Cron `python -m app.cli schedule-tick` (or admin tick) as needed
6. Leave YouTube OAuth empty until ready; privacy stays `private` by default
