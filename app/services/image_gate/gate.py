"""Image gate orchestration — score, revise, assert-before-HeyGen."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Awaitable, Callable
from uuid import UUID

from ...config import settings
from .budget import BudgetExhausted, consume_scorer_budget, ensure_budget_table
from .refs import ActiveReferenceSet, ReferenceSetError, load_active_reference_set
from .scorer import ScoreCard, ScorerError, score_candidate

log = logging.getLogger("media_state.image_gate")


class GateRefuse(RuntimeError):
    """Hard refuse — do not call HeyGen / Remotion."""


def evaluate_pass(card: ScoreCard) -> bool:
    return (
        float(card.overall) >= 8.0
        and float(card.identity_likeness) >= 8.0
        and card.identity_drift is False
    )


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256((prompt or "").encode()).hexdigest()


def _max_attempts() -> int:
    """Hard cap ≤3 regardless of misconfigured env."""
    return max(1, min(int(settings.image_gate_max_attempts or 3), 3))


def _job_meta(job) -> dict[str, Any]:
    meta = job["meta"] if job and "meta" in job.keys() else {}
    if isinstance(meta, str):
        meta = json.loads(meta or "{}")
    return dict(meta or {})


async def record_score(
    conn,
    *,
    job_id: UUID,
    attempt_n: int,
    card: ScoreCard,
    refs: ActiveReferenceSet,
    prompt: str,
    candidate_url: str | None = None,
    candidate_asset_id: UUID | None = None,
) -> dict[str, Any]:
    verdict = "pass" if evaluate_pass(card) else ("error" if card.scorer == "error" else "fail")
    reasons = list(card.failure_reasons)
    if card.overall < 8:
        reasons.append(f"overall={card.overall}<8")
    if card.identity_likeness < 8:
        reasons.append(f"identity_likeness={card.identity_likeness}<8")
    if card.identity_drift:
        reasons.append("identity_drift=true")
    # de-dupe
    reasons = list(dict.fromkeys(reasons))

    row = await conn.fetchrow(
        """
        INSERT INTO image_scores (
          job_id, attempt_n,
          face_consistency, lighting, composition, text_legibility,
          brand_fit, no_artifacts, identity_likeness, overall,
          identity_drift, verdict, scorer, failure_reasons,
          prompt_hash, candidate_asset_id, candidate_url,
          pack_id, manifest_id, hero_ref_file_id, ref_content_hash,
          likeness_approval_status, meta
        ) VALUES (
          $1,$2,
          $3,$4,$5,$6,
          $7,$8,$9,$10,
          $11,$12,$13,$14::text[],
          $15,$16,$17,
          $18,$19,$20,$21,
          $22,$23::jsonb
        )
        ON CONFLICT (job_id, attempt_n) DO UPDATE SET
          face_consistency = EXCLUDED.face_consistency,
          lighting = EXCLUDED.lighting,
          composition = EXCLUDED.composition,
          text_legibility = EXCLUDED.text_legibility,
          brand_fit = EXCLUDED.brand_fit,
          no_artifacts = EXCLUDED.no_artifacts,
          identity_likeness = EXCLUDED.identity_likeness,
          overall = EXCLUDED.overall,
          identity_drift = EXCLUDED.identity_drift,
          verdict = EXCLUDED.verdict,
          scorer = EXCLUDED.scorer,
          failure_reasons = EXCLUDED.failure_reasons,
          prompt_hash = EXCLUDED.prompt_hash,
          candidate_url = EXCLUDED.candidate_url,
          ref_content_hash = EXCLUDED.ref_content_hash,
          meta = EXCLUDED.meta
        RETURNING *
        """,
        job_id,
        attempt_n,
        card.face_consistency,
        card.lighting,
        card.composition,
        card.text_legibility,
        card.brand_fit,
        card.no_artifacts,
        card.identity_likeness,
        card.overall,
        card.identity_drift,
        verdict,
        card.scorer,
        reasons,
        _prompt_hash(prompt),
        candidate_asset_id,
        candidate_url,
        refs.pack_id,
        refs.manifest_id,
        refs.hero.filename,
        refs.content_hash,
        refs.likeness_approval_status,
        json.dumps({"raw": card.raw}),
    )
    await conn.execute(
        """
        UPDATE jobs
           SET image_score_id = $2,
               image_ref_content_hash = $3,
               image_attempt = GREATEST(COALESCE(image_attempt,0), $4)
         WHERE id = $1
        """,
        job_id,
        row["id"],
        refs.content_hash,
        attempt_n,
    )
    return dict(row)


async def _audit_break_glass(conn, job_id: UUID, meta: dict[str, Any]) -> None:
    await ensure_budget_table(conn)
    await conn.execute(
        """
        INSERT INTO image_gate_break_glass_audit
          (job_id, actor, reason, env_flag, job_flag, meta)
        VALUES ($1, $2, $3, true, true, $4::jsonb)
        """,
        job_id,
        str(meta.get("break_glass_actor") or meta.get("actor") or "break_glass"),
        str(meta.get("break_glass_reason") or "dual_control_break_glass"),
        json.dumps({"job_meta_keys": sorted(meta.keys()), "source": "assert_pass_for_heygen"}),
    )


async def assert_pass_for_heygen(conn, job_id: UUID) -> dict[str, Any]:
    """Must run in the same DB transaction as HeyGen key reserve.

    Break-glass is sealed: IMAGE_GATE_BREAK_GLASS env alone is NOT enough.
    Requires dual-control (env AND per-job meta.break_glass_image_gate) plus a
    durable audit row. Otherwise refuse — never silent bypass.
    """
    if settings.image_gate_break_glass:
        job = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1 FOR UPDATE", job_id)
        if not job:
            raise GateRefuse("job not found")
        meta = _job_meta(job)
        job_flag = bool(
            meta.get("break_glass_image_gate")
            or meta.get("image_gate_break_glass")
        )
        if not job_flag:
            log.error(
                "IMAGE_GATE_BREAK_GLASS env set but job %s lacks audited "
                "break_glass_image_gate flag — refusing",
                job_id,
            )
            raise GateRefuse(
                "break_glass refused: dual-control required "
                "(env IMAGE_GATE_BREAK_GLASS AND job meta.break_glass_image_gate) "
                "plus durable audit"
            )
        try:
            await _audit_break_glass(conn, job_id, meta)
        except Exception as exc:  # noqa: BLE001
            log.error("break_glass audit insert failed for %s: %s", job_id, exc)
            raise GateRefuse(
                f"break_glass refused: durable audit required but failed: {exc}"
            ) from exc
        log.error(
            "IMAGE_GATE_BREAK_GLASS dual-control AUDITED bypass for job %s",
            job_id,
        )
        return {
            "break_glass": True,
            "audited": True,
            "id": job.get("image_score_id"),
            "ref_content_hash": job.get("image_ref_content_hash"),
        }

    try:
        row = await conn.fetchrow("SELECT * FROM assert_image_pass_for_heygen($1)", job_id)
        if row:
            return dict(row)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if any(s in msg for s in ("P0004", "veto", "mismatch", "no passing", "not eligible", "ref_content")):
            raise GateRefuse(msg) from exc
        log.warning("RPC assert_image_pass_for_heygen failed (%s); python fallback", exc)

    try:
        refs = load_active_reference_set()
    except ReferenceSetError as exc:
        raise GateRefuse(str(exc)) from exc

    job = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1 FOR UPDATE", job_id)
    if not job:
        raise GateRefuse("job not found")
    score = await conn.fetchrow(
        """
        SELECT * FROM image_scores
         WHERE job_id=$1 AND verdict='pass'
         ORDER BY created_at DESC LIMIT 1
        """,
        job_id,
    )
    if not score:
        raise GateRefuse("no passing image_scores row — HeyGen refused")
    if float(score["overall"]) < 8 or float(score["identity_likeness"]) < 8 or score["identity_drift"]:
        raise GateRefuse(
            f"identity/overall veto overall={score['overall']} "
            f"identity={score['identity_likeness']} drift={score['identity_drift']}"
        )
    job_hash = job["image_ref_content_hash"]
    if not job_hash or score["ref_content_hash"] != job_hash:
        raise GateRefuse("ref_content_hash mismatch — re-score required")
    if score["ref_content_hash"] != refs.content_hash:
        raise GateRefuse("active reference set changed since score — re-score required")
    return dict(score)


def rewrite_prompt(prompt: str, failure_reasons: list[str], refs: ActiveReferenceSet) -> str:
    hints: list[str] = []
    joined = " ".join(failure_reasons).lower()
    if any(k in joined for k in ("identity", "likeness", "drift")):
        hints.append(
            f"Match hero identity exactly (file={refs.hero.filename}, sha={refs.hero.sha256[:12]}): "
            "same face geometry; do not redesign face."
        )
    if "lighting" in joined:
        hints.append("Match hero lighting direction and skin reflectance.")
    if "composition" in joined:
        hints.append("Keep framing consistent with hero_identity crop.")
    if "artifact" in joined:
        hints.append("No melted anatomy, plastic skin, or watermark artifacts.")
    if "text" in joined:
        hints.append("On-image text must be sharp and high-contrast.")
    if "brand" in joined:
        hints.append("Keep wardrobe/brand palette consistent with approved refs.")
    if not hints:
        hints.append("Improve likeness fidelity to hero_identity; keep face locked.")
    return f"{(prompt or '').strip()}\n\n[IMAGE_GATE_REVISE] {' '.join(hints)}".strip()


async def run_score_image_step(
    conn,
    job_id: UUID,
    *,
    candidate_url: str,
    prompt: str = "",
    forced_card: ScoreCard | None = None,
) -> dict[str, Any]:
    from ...state_machine import transition

    refs = load_active_reference_set()
    job = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1 FOR UPDATE", job_id)
    if not job:
        raise LookupError("job not found")

    await conn.execute(
        "UPDATE jobs SET image_ref_content_hash=$2 WHERE id=$1",
        job_id,
        refs.content_hash,
    )

    # Atomically increment BEFORE scoring (work_queue score_image / revise_image path)
    bumped = await conn.fetchrow(
        """
        UPDATE jobs
           SET image_attempt = COALESCE(image_attempt, 0) + 1,
               updated_at = now()
         WHERE id = $1
        RETURNING image_attempt
        """,
        job_id,
    )
    attempt_n = int(bumped["image_attempt"])
    max_att = _max_attempts()

    if attempt_n > max_att:
        await transition(
            conn,
            job_id,
            "needs_human_review",
            actor="image_gate",
            reason="max_image_attempts_exceeded",
            payload={"attempt_n": attempt_n, "max": max_att},
        )
        raise GateRefuse(
            f"image_attempt={attempt_n} exceeds max={max_att} — "
            "needs_human_review; HeyGen refused"
        )

    # Daily scorer budget — fail closed before any scorer / HeyGen spend
    try:
        await ensure_budget_table(conn)
        await consume_scorer_budget(conn)
    except BudgetExhausted as exc:
        err_card = ScoreCard(
            face_consistency=0,
            lighting=0,
            composition=0,
            text_legibility=0,
            brand_fit=0,
            no_artifacts=0,
            identity_likeness=0,
            overall=0,
            identity_drift=True,
            failure_reasons=[f"budget_exhausted:{exc}"],
            scorer="error",
        )
        await record_score(
            conn,
            job_id=job_id,
            attempt_n=attempt_n,
            card=err_card,
            refs=refs,
            prompt=prompt,
            candidate_url=candidate_url,
        )
        dest = "needs_human_review" if attempt_n >= max_att else "image_failed"
        await transition(
            conn,
            job_id,
            dest,
            actor="image_gate",
            reason="scorer_budget_exhausted",
            payload={"attempt_n": attempt_n},
        )
        raise GateRefuse(f"scorer budget fail-closed: {exc}") from exc

    try:
        card = await score_candidate(
            candidate_url=candidate_url, refs=refs, prompt=prompt, forced_card=forced_card
        )
    except (ScorerError, ReferenceSetError) as exc:
        dual_glass = settings.image_gate_break_glass and _job_meta(job).get(
            "break_glass_image_gate"
        )
        if settings.image_gate_fail_closed and not dual_glass:
            err_card = ScoreCard(
                face_consistency=0,
                lighting=0,
                composition=0,
                text_legibility=0,
                brand_fit=0,
                no_artifacts=0,
                identity_likeness=0,
                overall=0,
                identity_drift=True,
                failure_reasons=[f"scorer_error:{exc}"],
                scorer="error",
            )
            await record_score(
                conn,
                job_id=job_id,
                attempt_n=attempt_n,
                card=err_card,
                refs=refs,
                prompt=prompt,
                candidate_url=candidate_url,
            )
            dest = (
                "needs_human_review"
                if attempt_n >= max_att
                else "image_failed"
            )
            await transition(conn, job_id, dest, actor="image_gate", reason=f"scorer_error:{exc}")
            raise GateRefuse(f"scorer fail-closed: {exc}") from exc
        raise

    row = await record_score(
        conn,
        job_id=job_id,
        attempt_n=attempt_n,
        card=card,
        refs=refs,
        prompt=prompt,
        candidate_url=candidate_url,
    )
    if evaluate_pass(card):
        await transition(
            conn,
            job_id,
            "image_ready",
            actor="image_gate",
            reason="image_pass",
            payload={"image_score_id": str(row["id"]), "overall": card.overall},
        )
        return {"verdict": "pass", "score": dict(row), "status": "image_ready", "attempt_n": attempt_n}

    if attempt_n >= max_att:
        await transition(
            conn,
            job_id,
            "needs_human_review",
            actor="image_gate",
            reason="max_image_attempts",
            payload={"attempt_n": attempt_n, "reasons": card.failure_reasons},
        )
        return {"verdict": "fail", "score": dict(row), "status": "needs_human_review", "attempt_n": attempt_n}

    await transition(
        conn,
        job_id,
        "image_failed",
        actor="image_gate",
        reason="image_fail",
        payload={"attempt_n": attempt_n, "reasons": card.failure_reasons},
    )
    return {"verdict": "fail", "score": dict(row), "status": "image_failed", "attempt_n": attempt_n}


RegenerateFn = Callable[[UUID, str, ActiveReferenceSet], Awaitable[dict[str, Any]]]


def validate_regenerate_result(candidate: dict[str, Any] | None) -> str:
    """Fail closed: refuse mock/empty regenerate theater paths."""
    if not candidate or not isinstance(candidate, dict):
        raise GateRefuse(
            "revise_image: regenerator returned empty result — fail closed "
            "(no silent mock pass to HeyGen)"
        )
    candidate_url = (
        candidate.get("url") or candidate.get("candidate_url") or ""
    ).strip()
    if not candidate_url:
        raise GateRefuse(
            "revise_image: regenerator produced no candidate URL — fail closed"
        )
    if candidate_url.startswith("mock://"):
        raise GateRefuse(
            f"revise_image: refusing mock theater URL {candidate_url!r} — "
            "configure a real regenerator or supply revised_url"
        )
    return candidate_url


async def run_revise_image_step(
    conn,
    job_id: UUID,
    *,
    regenerate: RegenerateFn,
    forced_card: ScoreCard | None = None,
) -> dict[str, Any]:
    from ...state_machine import transition

    refs = load_active_reference_set()
    job = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1 FOR UPDATE", job_id)
    if not job:
        raise LookupError("job not found")
    if job["status"] not in ("image_failed",):
        raise GateRefuse(f"cannot revise from status {job['status']}")

    last = await conn.fetchrow(
        "SELECT * FROM image_scores WHERE job_id=$1 ORDER BY attempt_n DESC LIMIT 1",
        job_id,
    )
    meta = job["meta"]
    if isinstance(meta, str):
        meta = json.loads(meta or "{}")
    meta = dict(meta or {})
    prev_prompt = meta.get("image_prompt") or "Generate scene still of approved athlete avatar."
    reasons = list(last["failure_reasons"]) if last else ["prior_fail"]
    new_prompt = rewrite_prompt(prev_prompt, reasons, refs)
    meta["image_prompt"] = new_prompt
    meta["image_prompt_history"] = list(meta.get("image_prompt_history") or []) + [new_prompt]
    await conn.execute("UPDATE jobs SET meta=$2::jsonb WHERE id=$1", job_id, json.dumps(meta))

    if int(job["image_attempt"] or 0) >= _max_attempts():
        await transition(
            conn,
            job_id,
            "needs_human_review",
            actor="image_gate",
            reason="max_image_attempts",
            payload={"attempt_n": int(job["image_attempt"] or 0)},
        )
        raise GateRefuse("max image attempts already reached — revise refused")

    await transition(conn, job_id, "image_queued", actor="image_gate", reason="revise")
    await transition(
        conn,
        job_id,
        "image_running",
        actor="image_gate",
        reason="revise_claim",
        set_locked_by="image_gate",
    )

    candidate = await regenerate(job_id, new_prompt, refs)
    candidate_url = validate_regenerate_result(candidate)
    return await run_score_image_step(
        conn, job_id, candidate_url=candidate_url, prompt=new_prompt, forced_card=forced_card
    )
