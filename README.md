# Media State Engine

An atomic PostgreSQL state engine for vertical-video automation. It coordinates a script, ElevenLabs audio generation, Remotion rendering, and downstream delivery notifications without allowing illegal state changes or duplicate paid generation calls.

## Guarantees

Every state transition locks the job row with `SELECT ... FOR UPDATE`, validates the directed edge, inserts an event, updates `jobs.status`, and commits through the same database transaction. Illegal transitions return HTTP 409 and do not create events. Audio and render operations reserve `{job_id}:audio` and `{job_id}:render` before calling an external provider, so concurrent requests cannot double-spend. A reserved key with no result represents an interrupted operation and is returned as `in_progress`; operators can reconcile it rather than silently issuing a second provider call. The delivery webhook is sent only after the transaction that commits `rendered` has completed.

## Local setup

Use Python 3.11 or newer and a PostgreSQL database with `pgcrypto` available. Install dependencies, copy `.env.example` to `.env`, set `DATABASE_URL`, and run `schema.sql` against the database.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
psql "$DATABASE_URL" -f schema.sql
uvicorn app.main:app --reload
```

The service exposes `GET /health` and listens on port 8000 locally. Docker uses port 8080.

## Pipeline

Create a job with `POST /jobs`, advance it to `script_ready`, and call `POST /jobs/{id}/generate-audio`. After audio is committed, call `POST /jobs/{id}/render`. That endpoint commits the final asset and `rendered` status before calling `WEBHOOK_URL`. A delivery transition is explicit through `POST /jobs/{id}/advance` with `{"to_status":"delivered"}`.

The allowed edges are `pending → script_ready → audio_generating → audio_ready → rendering → rendered → delivered`, with `failed` reachable from any active state. `delivered` and `failed` are terminal.

## Configuration

The deployment requires `DATABASE_URL`, `ELEVENLABS_API_KEY`, `REMOTION_RENDER_URL`, `WEBHOOK_URL`, and `WEBHOOK_SECRET`. `ELEVENLABS_VOICE_ID` and `ELEVENLABS_URL` select the TTS voice and endpoint. The delivery request contains an HMAC-SHA256 signature in `x-webhook-signature`.

## Tests

```bash
pytest -q
```

The suite covers event-before-status semantics, illegal transitions producing zero events, and concurrent transition serialization. Production integration tests should run against an isolated PostgreSQL database and mock provider HTTP calls.

## Deployment

Railway can build from the included `Dockerfile` and use `railway.toml` for `/health` checks. Fly.io can use the same image. Run `schema.sql` once in Supabase before starting the service, then configure the variables in `.env.example`.


## Phase 1 hardening

Authenticated API access is required for job routes. Set `MEDIA_ENGINE_API_KEY` or `API_KEY` and send `Authorization: Bearer …` or `X-API-Key`. Health and `/webhooks/*` stay public; provider webhooks verify HMAC secrets (`HEYGEN_WEBHOOK_SECRET`, `ELEVENLABS_WEBHOOK_SECRET`, `REMOTION_WEBHOOK_SECRET`).

Outbound delivery uses a durable `webhook_outbox` with exponential backoff and dead-letter after N attempts. Stuck HeyGen jobs can be reconciled with `POST /jobs/{id}/reconcile` or `POST /admin/reconcile-stuck`. See `PHASE1_REPORT.md`.

## Phase 2 — background worker + pipeline

Provider I/O (ElevenLabs, HeyGen, Remotion) runs on a **Postgres `work_queue` worker**, not in the API process.

```bash
# API (enqueues work, returns 202)
uvicorn app.main:app --reload

# Worker (claims work_queue + flushes outbox)
python -m app.worker
```

- `POST /jobs/{id}/generate-audio|generate-avatar|render` → **202** `{work_id, step, queued}`
- `GET /jobs/{id}/detail` → assets, events, status history, work items, outbox
- HeyGen **Direct Video** (`POST /v3/videos`) with `callback_url` + `Idempotency-Key`
- Phase 1 reconcile remains for missed webhooks
- Outbox success can auto-advance `rendered → delivered`

See `PHASE2_REPORT.md` for queue choice, env vars, and stress-test notes.

## Phase 3 — content quality + approval gate

Short-form script generation stores structured `hook` / `body` / `cta` / duration on the job, stages TikTok/Reels/Shorts captions+overlays, and keeps **9:16** as the only default HeyGen render. Horizontal **16:9** is opt-in and does not burn a second HeyGen credit unless `HEYGEN_ALLOW_DUAL_FORMAT=true`.

After render, jobs land in **`staged`** (awaiting human approval). Nothing auto-publishes.

```bash
# Approve (API)
curl -X POST "$HOST/jobs/$ID/approve" -H "Authorization: Bearer $MEDIA_ENGINE_API_KEY" \
  -H 'content-type: application/json' -d '{"approved_by":"jordan"}'

# Approve (CLI)
python -m app.cli approve <job_id> --by jordan
```

Distribute in Phase 3 is a **stub** (`approved → delivered`, `public_post=false`). Real platform wire is Phase 4. See `PHASE3_REPORT.md`.
