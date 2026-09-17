# Phase 3 Report — Content Quality + Multi-Platform Staging

**Branch:** `feat/phase1-harden-core` (Phases 1–3 on one feature branch)  
**Date:** 2026-09-17 (PT)  
**Scope:** Structured short-form scripts, dual-format staging with HeyGen cost guardrail, platform caption/overlay fields, human approval gate before any public post. **Phase 4–5 not implemented.**

## Script schema / how generated

Jobs store a structured `script` JSONB column plus flat `script_text` (full spoken string for providers):

```json
{
  "hook": "Stop scrolling — <topic>!",
  "body": "…punchy mid…",
  "cta": "Follow for more…",
  "duration_target_seconds": 30,
  "aspect_ratio": "9:16",
  "full_text": "<hook> <body> <cta>",
  "source": "topic" | "script_text"
}
```

**Generation** (`app/services/scriptgen.py`):
- `POST /jobs` with `topic` and/or `script_text` (15–60s `duration_target_seconds`).
- Rule-based (no LLM / no extra API keys): topic → template hook/body/CTA; raw text → sentence split into hook / body / CTA.
- Optimized for TikTok/Reels/Shorts: strong first ~2s hook, punchy body, clear CTA, primary **9:16**.
- On create, job advances `pending → script_ready` when `auto_script_ready=true` (default).

**Platform captions / overlays** staged on `jobs.meta.platform_captions`:

```json
{
  "tiktok": {"caption": "…", "overlays": [{"t_start_s":0,"t_end_s":2,"text":"…","role":"hook"}, …], "hashtags":[…], "staged": true, "published": false},
  "reels": {…},
  "shorts": {…}
}
```

These are **staging fields only** — never sent to platforms in Phase 3.

## Dual-format approach + cost guardrail

| Mode | When | HeyGen calls | Behavior |
|------|------|--------------|----------|
| Primary only | default | **1** × 9:16 (1080×1920) | Horizontal `mode=not_requested` |
| Derived horizontal | `include_horizontal=true`, `HEYGEN_ALLOW_DUAL_FORMAT=false` | **1** | Stage 16:9 as `mode=derived` (letterbox/crop-from-primary later); **no second billable call** |
| Paid dual | `include_horizontal=true` **and** `HEYGEN_ALLOW_DUAL_FORMAT=true` | **2** | Second Direct Video 16:9 → assets `video_h` / `final_h` |

**Stress-test — dual-format HeyGen cost:**
- Default path never burns a second credit.
- Env flag alone is insufficient; job must also opt in.
- Paid dual failures on the horizontal call do **not** fail the primary job; horizontal meta records `status=failed`.
- Documented in `.env.example`: `HEYGEN_ALLOW_DUAL_FORMAT=false`.

## Approval flow (endpoint + CLI)

**Critical invariant:** nothing auto-publishes to TikTok / Reels / Shorts. Outbox success only notifies an *internal* review webhook.

```
… → rendering → rendered → staged ──(human)──► approved ──(stub)──► delivered
```

| Interface | How |
|-----------|-----|
| **HTTP** | `POST /jobs/{id}/approve` body `{approved_by?, enqueue_distribute?, note?}` (API key required) |
| **CLI** | `python -m app.cli approve <job_id> [--by NAME] [--no-distribute] [--note …]` |

Approve flips `staged → approved`, sets `approved_at` / `approved_by`, and may enqueue work step `distribute` (**Phase 3 stub**: advances `approved → delivered` with `public_post=false`; does **not** call platform APIs).

Refuse paths:
- `staged → delivered` is an illegal transition.
- `advance` to `delivered` from any status other than `approved` raises.
- `distribute_stub` raises unless status is `approved`.
- Outbox `_maybe_advance_delivered` is a **hard no-op** even if `AUTO_DELIVER_ON_OUTBOX_SUCCESS=true`.

## Status machine changes

```
pending → script_ready → audio_generating → audio_ready → rendering → rendered
  → staged → approved → delivered
failed reachable from any active state
```

- **Removed:** `rendered → delivered` (Phase 2 auto-happy-path publish shortcut).
- **Added:** `staged`, `approved`.
- After HeyGen/Remotion success: `rendered` then immediately `staged` in the same transaction; outbox payload carries `awaiting_approval: true`.

## Files changed / added

| Path | Action |
|------|--------|
| `app/services/scriptgen.py` | **New** — hook/body/cta + captions + formats |
| `app/cli.py` | **New** — `approve` / `show` / `script` |
| `app/state_machine.py` | staged/approved edges; no skip-to-delivered |
| `app/models.py` | JobCreate fields, ApproveRequest, new statuses/kinds |
| `app/config.py` | `heygen_allow_dual_format`; auto_deliver default false |
| `app/services/jobs.py` | structured create + `approve_job` |
| `app/services/pipeline.py` | stage-after-render; dual-format; `distribute_stub` |
| `app/services/outbox.py` | hard block auto-deliver/publish |
| `app/services/queue.py` | `distribute` step |
| `app/worker.py` | handle `distribute` |
| `app/api/jobs.py` | create body + `POST …/approve` |
| `schema.sql` / `migrations/003_phase3_content_quality.sql` | script/meta/approval + kinds + statuses |
| `.env.example` | Phase 3 env vars |
| `tests/test_phase3_quality.py` | **New** coverage |
| `tests/test_pipeline_phase1.py` | expect `staged` after HeyGen success |
| `tests/test_phase2_queue.py` | outbox must not auto-deliver |
| `PHASE3_REPORT.md` | This report |

## Env vars added

```
HEYGEN_ALLOW_DUAL_FORMAT=false
AUTO_DELIVER_ON_OUTBOX_SUCCESS=false
```

Secrets still env-only; never committed.

## Test results

```
43 passed, 2 warnings in ~1.8s
```

(Postgres via `DATABASE_URL=postgresql://media:media@127.0.0.1:5432/media_state`)

Coverage includes: script schema, dual-format single vs paid dual HeyGen call counts, state-machine block of `staged→delivered`, approve + distribute stub, approve HTTP auth shape, CLI approve, outbox non-publish.

## Stress-test notes

| Risk | Mitigation |
|------|------------|
| **Auto-publish** | No edge to `delivered` without `approved`; outbox cannot advance job; distribute stub checks status; captions marked `published: false` |
| **Double HeyGen burn** | Dual paid requires env **and** job opt-in; default is one 9:16 call + derived horizontal staging |
| **Skip approval via advance API** | `jobs.advance_job` rejects `delivered` unless current=`approved`; SM rejects `staged→delivered` |
| **Phase 2 regression** | Auth, outbox/DLQ, work queue, asset `video→final`, reconcile intact; happy path now ends at `staged` until approve |

## Blockers / environment

- **No live HeyGen / ElevenLabs / Remotion / platform keys** — provider I/O and distribute remain mocked/stubbed.
- **Phase 4** will wire real TikTok/Reels/Shorts publish behind the same approve gate.
- **Push/PR** not required for this phase; commit on feature branch only.
- Secrets never committed.

## Success criteria checklist

- [x] Script schema / generation documented
- [x] Dual-format approach + cost guardrail documented
- [x] Approval flow (endpoint + CLI)
- [x] Status machine changes
- [x] Tests pass + coverage for approve gate
- [x] PHASE3_REPORT.md
- [x] Commit on feature branch
- [x] Phase 4–5 **not** implemented
- [x] Honest blockers above
