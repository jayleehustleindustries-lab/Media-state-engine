# SHIP_REPORT — Media State Engine (schema SoT + image gate)

**Date:** 2026-09-17 PT  
**Branch:** `feat/phase1-harden-core`  
**Operator:** executor subagent (parent delivers to Jordan)

---

## What's built

### A) Schema SoT convergence (F1 — real, not a warning)
- Canonical migrations under `supabase/migrations/`:
  - `20260317000001_media_state_engine_core.sql` (from Drive pack 001)
  - `20260317000002_media_state_engine_auth_and_quality.sql` (from Drive pack 002; Supabase-role GRANTs patched for local/Postgres)
  - `20260317000003_app_adjuncts.sql` — work_queue, idempotency_keys, schedule_runs, metrics, provider_call_log
  - `20260317000004_image_gate.sql` — `image_scores`, image_* statuses, extended `transition_job` / `claim_job`, `assert_image_pass_for_heygen`
- `docs/DEPLOY.md` updated: **apply these packs**; legacy `schema.sql` marked LEGACY.
- App `state_machine.transition` calls `transition_job` RPC when new schema is live; dual-mode fallback preserves legacy Phase4/5 tests.

### B) Image quality gate (non-negotiable)
| Piece | Path |
|-------|------|
| Reference pack (local) | `reference_images/ACTIVE_REFERENCE_SET.json` + 8 locked refs |
| Drive mirror docs | `docs/REFERENCE_IMAGES.md` |
| Loader (fail-closed) | `app/services/image_gate/refs.py` |
| Scorer Gemini→Grok | `app/services/image_gate/scorer.py` |
| Gate / revise / assert | `app/services/image_gate/gate.py` |
| Worker steps | `score_image`, `revise_image` in `queue.STEPS` + `worker.py` |
| HeyGen hard stop | `pipeline.generate_avatar` calls `assert_pass_for_heygen` **before** idempotency reserve + `heygen.create_video` |

**Pass rule (exact):** `overall ≥ 8` **AND** `identity_likeness ≥ 8` **AND** `identity_drift = false`.  
Overall ≥8 alone is **insufficient** (audit C1).

**Axes (1–10):** face_consistency, lighting, composition, text_legibility, brand_fit, no_artifacts, identity_likeness → overall.

**Loop:** fail → rewrite prompt from failure_reasons → regenerate → re-score; max **3** attempts → `needs_human_review`. Never silent ship.

**Fail closed:** scorer errors → no HeyGen; empty/unapproved ref set → refuse; ref content hash bind on every score; hash mismatch invalidates pass.

**Config:** `IMAGE_GATE_MAX_ATTEMPTS=3`, `IMAGE_GATE_FAIL_CLOSED=true`, `IMAGE_GATE_BREAK_GLASS=false`, `IMAGE_SCORER_*`, `ACTIVE_REFERENCE_SET_PATH`.

### C) Phase 4 F2–F5 (verified in code; still present)
| ID | Status | Evidence |
|----|--------|----------|
| F1 Schema SoT | **Done** | migrations 001–004 installed; DEPLOY points at them |
| F2 Schedule lock | **Holds** | `pg_try_advisory_lock` for whole tick in `scheduler.py` |
| F3 YouTube idempotency | **Holds** | `_existing_youtube_upload_id` / `persist_youtube_upload_id` short-circuit |
| F4 Reclaim | **Holds** | `reclaim_stale_running` + `reclaim_stale_delivering` in every worker tick |
| F5 Advance→approved block | **Holds** | `jobs.advance_job` rejects `approved`/`delivered`; use `/approve` |

Approval gate remains sacred: **no auto-publish**.

### D) Phase 5 ship surfaces
- `docker-compose.yml` — api + worker + Postgres
- `scripts/apply_migrations.sh`
- `.env.example` image-gate keys
- Dockerfile / railway.toml (pre-existing)

---

## Tests

| Suite | Result |
|-------|--------|
| `tests/test_image_gate.py` | **9 passed** |
| Full legacy Phase4/5 on same DB | Conflicts when both schemas share one DB (expected during dual-mode cutover) |

Image-gate coverage:
1. Load locked pack  
2. Empty set refuse  
3. Unapproved likeness refuse  
4. Identity veto (overall 9 / identity 7.5 fails)  
5. Pass at 8+ → assert allows HeyGen path  
6. Fail→retry→needs_human_review at max attempts  
7. HeyGen mock never called on gate refuse  
8. Ref hash change invalidates pass  
9. Motion/texture never sole identity  

---

## How to run

```bash
# Schema
createdb media_state   # or docker compose up db
./scripts/apply_migrations.sh

# App
cp .env.example .env   # set MEDIA_ENGINE_API_KEY; leave provider keys empty to mock
pip install -r requirements.txt
uvicorn app.main:app --reload
python -m app.worker

# Tests (image gate)
DATABASE_URL=postgresql:///media_state pytest tests/test_image_gate.py -q
```

---

## Scoring rubric (ship)

| Axis | Floor for pass |
|------|----------------|
| overall | ≥ 8 |
| identity_likeness | ≥ 8 (hard veto) |
| identity_drift | must be false |
| face_consistency, lighting, composition, text_legibility, brand_fit, no_artifacts | scored 0–10; feed overall |

Brutal edge cases closed:
- Pretty/sharp wrong face (identity veto)
- Scorer timeout/exception (fail closed)
- Empty / unapproved ref pack (refuse)
- Ref pack updated mid-job (hash mismatch)
- Max 3 attempts → `needs_human_review` (no silent continue)
- HeyGen dual-format second call still gated by same assert
- `IMAGE_GATE_BREAK_GLASS` defaults false (ops-only)

---

## One thing still manual

1. **Live Gemini/Grok API keys** — scorers are wired; without keys, tests use `forced_card`. Production needs `IMAGE_SCORER_GEMINI_API_KEY` or `GEMINI_API_KEY` (Grok fallback via `XAI_API_KEY`).
2. **Full cutover of pipeline status strings** — worker/pipeline still has legacy status names in places; RPC dual-mode bridges, but a follow-up should delete legacy `schema.sql` path entirely and rebaseline Phase4/5 tests onto 001–004.
3. **Push/PR** — depends on git credentials in this environment.

---

## Confidence

**7 / 10** — Image gate + schema packs are real and tested; F2–F5 code paths intact; remaining risk is incomplete status-string cutover across every pipeline branch and shared-DB test isolation.

## Hard blockers

- No live vision API keys in env (expected).
- No guarantee of `git push` auth from this box.
- Legacy vs new schema tests must not share one DB without re-migrate.

---

## Commit intent

Single ship commit (or small series) on `feat/phase1-harden-core` with migrations, image_gate package, wiring, tests, docs, SHIP_REPORT.
