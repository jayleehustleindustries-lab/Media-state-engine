# Phase 5 Report — Deploy docs, root cleanup, audit F1–F5

**Branch:** `feat/phase1-harden-core`  
**Date:** 2026-09-17 (PT)  
**Base:** Phases 1–4 @ `a9a1a21`  
**Scope:** Deploy/SoT documentation, remove root module duplicates, fix audit
findings F1–F5 (priority order). No push / no PR (parent handles auth).

## Done

### Cleanup
- Deleted unused root `config.py`, `db.py`, `models.py` (app uses `app.*`;
  root `db.py` only imported root `config`).

### Deploy + SoT (F1)
- `Dockerfile` documents alternate worker CMD; `railway.toml` notes second
  service + `startCommand` for API.
- Added [`docs/DEPLOY.md`](docs/DEPLOY.md): apply `schema.sql` then
  `migrations/*.sql`; API vs worker env; `/health`; **explicit warning** not
  to apply Drive/Supabase packs (`001_media_state_engine_core.sql`, etc.).
- Banner comment at top of `schema.sql`.
- README refreshed to match Phases 1–4 status graph + Phase 5 deploy/SoT;
  links `PHASE*_REPORT.md`; removed stale Remotion-only / old edge wording.

### Audit code fixes
| ID | Fix |
|----|-----|
| **F5** | `advance_job` rejects `to_status in ('approved','delivered')`; API returns 409. Require `/approve` + distribute worker. |
| **F1** | Docs + `schema.sql` SoT banner (no schema convergence). |
| **F4** | `queue.reclaim_stale_running` + `outbox.reclaim_stale_delivering`; wired into every `worker.tick`; `POST /admin/reclaim-stale`. Config: `WORK_QUEUE_STALE_SECONDS`, `OUTBOX_STALE_SECONDS` (default 900). |
| **F3** | Persist `youtube_upload_id` ASAP after live upload; short-circuit YouTube `publish` when id already in meta; `distribute` idempotent if already `delivered`. |
| **F2** | Day-level `pg_try_advisory_lock` held for whole `scheduler.tick`; reserve `schedule_runs` slot before `create_job` to avoid orphans. |

## Deferred (out of Phase 5 priority / time-box)
- F6–F13 from independent audit (quality gate, enqueue_avatar override, caption
  `published` vs privacy, compare_digest, docs auth, force tick limits, empty
  webhook secret, provider spend caps) — not implemented here.

## Tests
- Added `tests/test_phase5_audit.py` (F5 HTTP+DB, F4 reclaim, F3 idempotency, F2 concurrent ticks).
- Full suite with local Postgres: **58 passed**.

## Ops notes
- Deploy API + worker from the same image (see `docs/DEPLOY.md`).
- Never apply supabase-schema packs to this service’s database.
