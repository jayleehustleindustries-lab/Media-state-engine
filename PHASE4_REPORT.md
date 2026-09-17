# Phase 4 Report — Scheduling + Distribution

**Branch:** `feat/phase1-harden-core` (Phases 1–4 on one feature branch)  
**Date:** 2026-09-17 (PT)  
**Scope:** Daily cadence scheduler, YouTube Shorts distributor (credential-gated), staging export, metrics. **Phase 5 not implemented.** Approval gate from Phase 3 is intact — **nothing public-posts without explicit approve.**

## Scheduler how-to

Daily cadence defaults to **3 posts/day** (`SCHEDULE_POSTS_PER_DAY`) in `America/Los_Angeles`.

### Cron-friendly CLI

```bash
export DATABASE_URL=postgresql://...
# optional: SCHEDULE_POSTS_PER_DAY=3 SCHEDULE_TOPICS="topic a,topic b,topic c"

# Fill remaining slots for today (idempotent under the daily cap)
python -m app.cli schedule-tick

# Force create N jobs ignoring cap (ops / backfill)
python -m app.cli schedule-tick --count 2 --force --topics "hook topic one,hook topic two"

# Inspect fill
python -m app.cli schedule-status
```

Example crontab (local PT box or container with TZ set):

```cron
# Three spaced ticks; each only creates remaining slots for the day
0 8,12,17 * * * cd /app && . .venv/bin/activate && python -m app.cli schedule-tick
```

### In-process admin tick

```bash
curl -X POST "$HOST/admin/scheduler/tick" \
  -H "Authorization: Bearer $MEDIA_ENGINE_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"count": null, "force": false}'

curl "$HOST/admin/scheduler/status" -H "Authorization: Bearer $MEDIA_ENGINE_API_KEY"
```

**What schedule does / does not do**

| Does | Does not |
|------|----------|
| Create `script_ready` jobs + `schedule_runs` rows | Render or public-post |
| Optionally enqueue `generate_avatar` if `SCHEDULE_ENQUEUE_AVATAR=true` | Bypass approve |
| Respect daily cap unless `--force` | Call TikTok / Reels / YouTube |

Worker still required for avatar/render/distribute. Approve still required before distribute.

## Platform wiring

| Platform | Status |
|----------|--------|
| **YouTube Shorts** | **Wired** — `YouTubeDistributor` uses Data API v3 resumable upload when `YOUTUBE_CLIENT_ID` + `YOUTUBE_CLIENT_SECRET` + `YOUTUBE_REFRESH_TOKEN` are set; otherwise **staging-only no-op** (no network). Default privacy `YOUTUBE_PRIVACY_STATUS=private` so even live uploads are not public unless explicitly set to `public`. |
| TikTok | **Documented-only** — captions staged; manual upload via `data/staging/{job_id}/` |
| Instagram Reels | **Documented-only** — same staging package |
| YouTube Shorts (manual) | Staging `shorts_caption.txt` + `manifest.json` always written |
| Generic Shorts | Covered by staging export |

### Distribute flow (approval-gated)

```
staged ──(human approve)──► approved ──(worker distribute)──► delivered
```

1. Always writes staging package under `DISTRIBUTE_STAGING_DIR` (default `data/staging`).
2. Calls YouTube only if OAuth env present **and** a local `final` asset file exists.
3. `public_post=true` **only** when a live YouTube upload succeeded with `privacy=public`.
4. `platform_captions.*.published` flipped **only** for platforms that actually went live (`shorts`/`youtube`); TikTok/Reels stay `published: false`.

Refuse paths unchanged: distribute raises unless status is `approved`; SM has no `staged→delivered` edge; outbox cannot auto-publish.

## Metrics endpoints / fields

| Endpoint | Auth |
|----------|------|
| `GET /metrics` | API key |
| `GET /admin/metrics` | API key |
| CLI `python -m app.cli metrics` | DB only |

**Counters** (`metric_counters`): `jobs_created`, `jobs_rendered`, `jobs_staged`, `jobs_delivered`, `jobs_failed`, `jobs_distributed_live`, `jobs_distributed_staging`, `schedule_jobs_created`.

**Latency samples** (`metric_samples`): provider name → ms (e.g. `youtube`).

**Cost estimate** on `jobs.meta.cost_estimate`:

```json
{
  "currency": "USD",
  "total_usd_estimate": 0.5,
  "items": [{"provider": "heygen", "calls": 1, "usd": 0.5}],
  "note": "Rough operator estimate — not billed amounts"
}
```

Snapshot also includes `jobs_by_status` histogram.

## Files changed / added

| Path | Action |
|------|--------|
| `app/services/distribute/` | **New** — base, staging export, YouTube distributor |
| `app/services/scheduler.py` | **New** — daily cadence tick/status |
| `app/services/metrics.py` | **New** — counters / samples / snapshot |
| `app/services/pipeline.py` | Real `distribute` (+ stub alias); metrics hooks |
| `app/services/jobs.py` | Approve notes; create counter |
| `app/config.py` | Schedule + YouTube + staging settings |
| `app/cli.py` | `schedule-tick`, `schedule-status`, `metrics` |
| `app/api/jobs.py` | `/admin/scheduler/*`, `/metrics`, `/admin/metrics` |
| `app/worker.py` | Calls `pipeline.distribute` |
| `app/main.py` | version 1.4.0 |
| `schema.sql` / `migrations/004_phase4_schedule_distribute.sql` | `schedule_runs`, metrics tables |
| `.env.example` | Phase 4 env vars |
| `tests/test_phase4_schedule_distribute.py` | **New** |
| `tests/test_phase3_quality.py` | Distribute assertion updated for Phase 4 |
| `tests/conftest.py` | Drop/recreate Phase 4 tables |
| `PHASE4_REPORT.md` | This report |
| `README.md` | Phase 4 section |

## Env vars added

```
SCHEDULE_POSTS_PER_DAY=3
SCHEDULE_TIMEZONE=America/Los_Angeles
SCHEDULE_TOPICS=
SCHEDULE_DURATION_SECONDS=30
SCHEDULE_PLATFORMS=tiktok,reels,shorts
SCHEDULE_ENQUEUE_AVATAR=false
DISTRIBUTE_STAGING_DIR=data/staging
YOUTUBE_CLIENT_ID=
YOUTUBE_CLIENT_SECRET=
YOUTUBE_REFRESH_TOKEN=
YOUTUBE_PRIVACY_STATUS=private
```

**Never commit secrets.** YouTube OAuth tokens stay in env / secret store only.

## Test results

```
53 passed, 2 warnings in ~2.3s
```

(`DATABASE_URL=postgresql://media:media@127.0.0.1:5432/media_state`)

Coverage includes: schedule daily cap + force, distribute refuse without approve, staging-only captions unpublished, live YouTube mock sets `shorts.published` + `public_post`, metrics auth endpoints, YouTube not-configured path, CLI help for schedule-tick.

## Stress-test notes

| Risk | Mitigation |
|------|------------|
| **Auto-publish** | Approve still required; distribute refuses non-`approved`; TikTok/Reels never called; YouTube default privacy `private`; `public_post` only after live+public success |
| **What burns money** | HeyGen when worker runs `generate_avatar` (scheduler only enqueues if `SCHEDULE_ENQUEUE_AVATAR=true`); ElevenLabs/Remotion on those steps; YouTube upload itself is free API quota but needs OAuth. Dual HeyGen still gated by Phase 3 flag |
| **What's stubbed** | TikTok / Reels APIs (documented-only); YouTube live path no-ops without creds or without local MP4 bytes; HeyGen/ElevenLabs/Remotion still mocked in tests |
| **Missing local MP4** | HeyGen often stores URL only — live YouTube falls back to staging_only until bytes are persisted (Phase 5+ persistence) |
| **Cadence double-create** | `schedule_runs(run_date, slot)` unique + daily remaining math |
| **Phase 1–3 regression** | Auth, outbox/DLQ, worker, asset kinds, reconcile, approve gate intact |

## Blockers / environment

- **No YouTube OAuth credentials** in this environment — live upload covered by unit/mock; expected blocker for real Shorts publish.
- **No local HeyGen MP4 persistence** yet — live YouTube needs a file on disk; URL-only assets → staging export.
- **No TikTok / Meta Reels API apps** configured — intentionally documented-only.
- **Phase 5** (deploy cleanup README PR) **not** done.
- Secrets never committed.

## Success criteria checklist

- [x] Daily cadence (CLI + admin tick) + how-to documented
- [x] One platform wired (YouTube) + others documented
- [x] Staging + approval gate; `public_post` / published flags careful
- [x] Metrics counters + latency + cost fields + GET endpoints
- [x] No secrets committed
- [x] PHASE4_REPORT.md with stress-test notes
- [x] Tests pass + commit on feature branch
- [x] Phase 5 **not** implemented
