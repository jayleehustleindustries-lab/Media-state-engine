# Phase 1 Report — Harden Core (P0)

**Branch:** `feat/phase1-harden-core`  
**Date:** 2026-09-17 (PT)  
**Scope:** API auth, stuck-step recovery, webhook outbox/DLQ, inbound webhook lockdown, asset kinds + audio persistence, real Postgres concurrency tests.

## What changed

### 1. API auth
- Middleware `ApiKeyMiddleware` + FastAPI dependency `require_api_key`.
- **Header:** `Authorization: Bearer <key>` **or** `X-API-Key: <key>`
- **Env:** `MEDIA_ENGINE_API_KEY` (preferred) or `API_KEY`
- **Public:** `GET /health`, `/docs`, `/openapi.json`, `/webhooks/*`
- **Protected:** all `/jobs*` mutations/reads and `/admin/*`
- Fail-closed when no API key is configured (503) so unpaid/open deployments cannot burn providers.
- Inbound provider webhooks **do not** use the API key.

### 2. Stuck-step recovery + fail-on-provider-error
- Provider exceptions in `generate_audio` / `render` / `generate_avatar` → job `failed` + event + **idempotency key cleared**.
- `POST /jobs/{id}/reconcile` — calls HeyGen `get_video` for jobs in `rendering` with a `kind=video` HeyGen asset; applies terminal success/fail via the same path as webhooks.
- `POST /admin/reconcile-stuck?older_than_seconds=&limit=` — batch reconcile for stale `rendering` / `audio_generating` jobs (`STUCK_JOB_SECONDS`, default 900).
- **Poisoned idempotency recovery:**
  - Unfinished keys get `expires_at` (`IDEMPOTENCY_TTL_SECONDS`, default 3600).
  - `get_key` deletes expired unfinished keys so retries can re-reserve.
  - Keys are **cleared on fail** (not left as hollow `in_progress`).
  - Completed keys (`result` set) remain permanent double-spend guards.

### 3. Webhook outbox + retries + DLQ
- Table `webhook_outbox` (`pending` → `delivering` → `delivered` | `dead`).
- Enqueued **in the same DB transaction** as `rendered` commits (Remotion render + HeyGen success).
- Exponential backoff (`WEBHOOK_OUTBOX_BASE_DELAY_SECONDS * 2^(attempts-1)`, cap 1h).
- Dead-letter after `WEBHOOK_OUTBOX_MAX_ATTEMPTS` (default 8).
- `POST /admin/outbox/flush` processes due rows.
- Delivery is never a silent post-commit fire-and-forget without a durable row.

### 4. Inbound webhook lockdown
- HeyGen: existing HMAC-SHA256 `signature` header (`HEYGEN_WEBHOOK_SECRET`).
- ElevenLabs: HMAC `x-webhook-signature` / `signature` (`ELEVENLABS_WEBHOOK_SECRET`).
- Remotion: same pattern (`REMOTION_WEBHOOK_SECRET`).
- Missing secret → 503; bad/missing signature → 401.

### 5. Asset kinds + audio persistence
- HeyGen in-progress insert uses **`kind=video`** (not `final`).
- Webhook/reconcile success inserts/upserts **`kind=final`** for the delivered URL; video row retained.
- ElevenLabs writes audio bytes under `ASSET_STORAGE_DIR` (default `data/assets`) via `app/services/storage.py`.

### 6. Real Postgres concurrency tests
- `tests/test_concurrency_pg.py` and `tests/test_pipeline_phase1.py` use real `asyncpg` + `DATABASE_URL`.
- Skip clearly via `require_database_url` if unset.
- FakeConn retained **only** for unit transition legality tests — **not** for concurrency claims.

## Files touched / added

| Path | Action |
|------|--------|
| `app/config.py` | Auth, outbox, TTL, storage, stuck settings |
| `app/auth.py` | **New** — middleware + dependency |
| `app/main.py` | Wire middleware, version 1.1.0 |
| `app/api/jobs.py` | Auth deps, reconcile, outbox flush, inbound HMAC |
| `app/services/pipeline.py` | Fail-on-error, video/final kinds, reconcile |
| `app/services/jobs.py` | TTL reserve/get, clear_key |
| `app/services/webhooks.py` | Outbox enqueue + flush |
| `app/services/outbox.py` | **New** — outbox/DLQ |
| `app/services/storage.py` | **New** — local byte storage |
| `app/services/elevenlabs.py` | Persist audio bytes |
| `schema.sql` | `webhook_outbox`, `expires_at`, indexes |
| `migrations/001_phase1_harden.sql` | **New** migration |
| `.env.example` | New env vars |
| `.gitignore` | `.venv`, `.env`, `data/` |
| `tests/*` | Auth, outbox, storage, PG concurrency, pipeline |
| `PHASE1_REPORT.md` | This report |

## How auth works
- Present `Authorization: Bearer <token>` or `X-API-Key: <token>`.
- Token must equal `MEDIA_ENGINE_API_KEY` or `API_KEY` (former wins if both set).

## How outbox + DLQ works
1. State transition commits and inserts `webhook_outbox` in one transaction.
2. Best-effort `flush_outbox` runs after commit.
3. Failures leave row `pending` with `next_attempt_at` backoff.
4. After `max_attempts`, status=`dead` (operator must inspect / replay).

## How reconcile works
1. Auth required: `POST /jobs/{id}/reconcile`.
2. Loads HeyGen `video` asset → `heygen.get_video(video_id)`.
3. Terminal fail statuses → `failed` + clear idempotency + event.
4. Terminal success / `video_url` present → same as success webhook (`final` asset + outbox).
5. Still processing → `{reconciled: false}`.

## Asset kind fix confirmation
- Covered by `test_generate_avatar_uses_kind_video` and `test_heygen_success_promotes_final` (real PG).

## Test results
Ran with local Postgres 17 (`DATABASE_URL=postgresql://media:media@127.0.0.1:5432/media_state`):

```
22 passed, 2 warnings in 0.87s
```

No skips when `DATABASE_URL` is set. Without it, PG-marked tests skip via fixture.

## Stress-test notes
- Concurrent `advance` and idempotency `reserve_key` serialized correctly under real PG (`FOR UPDATE` / unique key).
- Outbox: forced delivery failures → retry then `dead` after max attempts; row survives commit.
- Provider error path clears idempotency so a later retry can re-call HeyGen without poisoning.

## Still open for Phase 2+
- Background outbox worker / cron (today: on-demand flush + post-commit kick).
- Replay tooling for `dead` outbox rows; metrics/alerts.
- Download & persist HeyGen final MP4 bytes (currently URL + metadata).
- Remotion/ElevenLabs end-to-end with real credentials.
- Optional key rotation / multi-key support.
- Admin UI / ops dashboard for stuck jobs and DLQ.
- Stronger schema migration runner (only raw SQL file today).

## Blockers / environment
- **No Docker** in this box; Postgres installed via apt and started with `pg_ctlcluster` for tests.
- **No live HeyGen / ElevenLabs / Remotion API keys** in env — provider I/O covered by mocks; do not invent credentials.
- **Push/PR:** attempt after commit; success depends on `gh` / remote credentials.
- Secrets never committed; `.env` gitignored.

## Success criteria checklist
- [x] Files changed listed
- [x] Auth header + env documented
- [x] Outbox + DLQ documented
- [x] Reconcile documented
- [x] Asset kind fix confirmed in tests
- [x] Test results recorded
- [x] Branch + commit (see git log)
- [x] Honest blockers above
