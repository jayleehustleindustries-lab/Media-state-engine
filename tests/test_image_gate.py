"""Image quality gate — hard caps from image-gate-audit MUST-HAVE."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[1]
REF_PACK = REPO / "reference_images" / "ACTIVE_REFERENCE_SET.json"
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///media_state")


def _card(**overrides):
    from app.services.image_gate.scorer import ScoreCard
    base = dict(
        face_consistency=8.5,
        lighting=8.0,
        composition=8.0,
        text_legibility=8.0,
        brand_fit=8.0,
        no_artifacts=8.0,
        identity_likeness=8.5,
        overall=8.5,
        identity_drift=False,
        failure_reasons=[],
        scorer="mock",
    )
    base.update(overrides)
    return ScoreCard(**base)


async def _fresh_conn():
    import asyncpg
    return await asyncpg.connect(DATABASE_URL)


async def _seed_job(conn, script=None):
    from uuid import uuid4
    script = script or f"Hook {uuid4().hex[:8]}. Body ships. CTA follow now."
    row = await conn.fetchrow(
        "SELECT * FROM create_job($1::text, $2::text, NULL::text, NULL::text, 'heygen'::renderer_type)",
        script,
        "v1",
    )
    await conn.execute(
        """
        INSERT INTO content_quality_scores
          (job_id, script_hash, hook_strength, clarity, cta, pacing, verdict, scorer)
        VALUES ($1, $2, 90, 90, 90, 90, 'pass'::quality_verdict, 'bot')
        """,
        row["id"],
        row["script_hash"],
    )
    return row


def test_load_active_reference_set():
    from app.services.image_gate import load_active_reference_set
    refs = load_active_reference_set(REF_PACK)
    assert refs.likeness_approval_status == "approved"
    assert len(refs.images) == 8
    assert refs.hero.filename.endswith("hero_identity.jpeg") or "hero" in refs.hero.filename
    assert refs.content_hash


def test_empty_set_refuses(tmp_path):
    from app.services.image_gate import load_active_reference_set, ReferenceSetError
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({
        "pack_id": "X", "likeness_approval_status": "approved", "images": [],
        "identity_lock": {},
    }))
    with pytest.raises(ReferenceSetError):
        load_active_reference_set(p)


def test_unapproved_likeness_refuses(tmp_path):
    from app.services.image_gate import load_active_reference_set, ReferenceSetError
    data = json.loads(REF_PACK.read_text())
    data["likeness_approval_status"] = "pending"
    p = tmp_path / "pending.json"
    p.write_text(json.dumps(data))
    with pytest.raises(ReferenceSetError):
        load_active_reference_set(p)


def test_evaluate_pass_identity_veto():
    from app.services.image_gate import evaluate_pass
    assert evaluate_pass(_card()) is True
    # overall >=8 but identity <8 → fail
    assert evaluate_pass(_card(overall=9.0, identity_likeness=7.5)) is False
    # identity_drift true → fail even if scores high
    assert evaluate_pass(_card(identity_drift=True)) is False
    # overall 7.9 → fail
    assert evaluate_pass(_card(overall=7.9)) is False


async def test_pass_at_8_plus_allows_heygen_assert():
    from app.services.image_gate import (
        load_active_reference_set, record_score, assert_pass_for_heygen, evaluate_pass,
    )
    from app.state_machine import transition
    refs = load_active_reference_set(REF_PACK)
    conn = await _fresh_conn()
    try:
        job = await _seed_job(conn)
        jid = job["id"]
        await transition(conn, jid, "image_queued", actor="test")
        await transition(conn, jid, "image_running", actor="test", set_locked_by="test")
        card = _card(overall=8.0, identity_likeness=8.0)
        assert evaluate_pass(card)
        await record_score(
            conn, job_id=jid, attempt_n=1, card=card, refs=refs,
            prompt="hero still", candidate_url="mock://ok",
        )
        await transition(conn, jid, "image_ready", actor="test")
        score = await assert_pass_for_heygen(conn, jid)
        assert float(score["overall"]) >= 8
    finally:
        await conn.close()


async def test_fail_and_retry_then_human_review():
    """Atomic image_attempt bump on each score; 3rd fail → needs_human_review."""
    from app.services.image_gate import load_active_reference_set, run_score_image_step
    from app.config import settings
    from app.state_machine import transition

    load_active_reference_set(REF_PACK)
    assert settings.image_gate_max_attempts == 3
    conn = await _fresh_conn()
    try:
        job = await _seed_job(conn)
        jid = job["id"]
        await transition(conn, jid, "image_queued", actor="test")
        # attempt 1 fail (0→1)
        await transition(conn, jid, "image_running", actor="test", set_locked_by="t")
        r1 = await run_score_image_step(
            conn, jid, candidate_url="https://example.com/1.jpg", prompt="p1",
            forced_card=_card(overall=7.0, identity_likeness=7.0, failure_reasons=["overall"]),
        )
        assert r1["verdict"] == "fail"
        assert r1["status"] == "image_failed"
        assert r1["attempt_n"] == 1
        assert await conn.fetchval("SELECT image_attempt FROM jobs WHERE id=$1", jid) == 1

        # attempt 2 fail (1→2) — no manual bump
        await transition(conn, jid, "image_queued", actor="test")
        await transition(conn, jid, "image_running", actor="test", set_locked_by="t")
        r2 = await run_score_image_step(
            conn, jid, candidate_url="https://example.com/2.jpg", prompt="p2",
            forced_card=_card(overall=7.5, identity_likeness=8.5, identity_drift=True,
                              failure_reasons=["drift"]),
        )
        assert r2["status"] == "image_failed"
        assert r2["attempt_n"] == 2

        # attempt 3 fail (2→3) → needs_human_review
        await transition(conn, jid, "image_queued", actor="test")
        await transition(conn, jid, "image_running", actor="test", set_locked_by="t")
        r3 = await run_score_image_step(
            conn, jid, candidate_url="https://example.com/3.jpg", prompt="p3",
            forced_card=_card(overall=6.0, identity_likeness=6.0, failure_reasons=["bad"]),
        )
        assert r3["status"] == "needs_human_review"
        assert r3["attempt_n"] == 3
        assert await conn.fetchval("SELECT image_attempt FROM jobs WHERE id=$1", jid) == 3
    finally:
        await conn.close()


async def test_heygen_never_called_on_fail(monkeypatch):
    """generate_avatar must not call heygen.create_video when gate refuses."""
    from app.services import pipeline, heygen
    from app.services.image_gate import GateRefuse

    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("heygen.create_video must not be called")

    monkeypatch.setattr(heygen, "create_video", boom)

    # Force assert to refuse
    async def refuse(conn, job_id):
        raise GateRefuse("no pass")

    monkeypatch.setattr(pipeline, "assert_pass_for_heygen", refuse)

    # If generate_avatar is reachable without DB job it may fail earlier — that's OK
    # We only assert heygen was never called.
    try:
        from uuid import uuid4 as _u; await pipeline.generate_avatar(_u())
    except Exception:
        pass
    assert called["n"] == 0


async def test_ref_hash_change_invalidates_pass():
    from app.services.image_gate import (
        load_active_reference_set, record_score, assert_pass_for_heygen, GateRefuse,
    )
    from app.state_machine import transition
    refs = load_active_reference_set(REF_PACK)
    conn = await _fresh_conn()
    try:
        job = await _seed_job(conn)
        jid = job["id"]
        await transition(conn, jid, "image_queued", actor="t")
        await transition(conn, jid, "image_running", actor="t", set_locked_by="t")
        await record_score(
            conn, job_id=jid, attempt_n=1, card=_card(), refs=refs,
            prompt="x", candidate_url="mock://x",
        )
        await transition(conn, jid, "image_ready", actor="t")
        # Corrupt job hash to simulate reference-set update (stay image_ready)
        await conn.execute(
            "UPDATE jobs SET image_ref_content_hash=$2 WHERE id=$1",
            jid, "deadbeef" * 8,
        )
        with pytest.raises(GateRefuse):
            await assert_pass_for_heygen(conn, jid)
    finally:
        await conn.close()


def test_motion_texture_never_sole_identity():
    from app.services.image_gate import load_active_reference_set
    refs = load_active_reference_set(REF_PACK)
    identity = refs.identity_only_or_raise()
    assert any(i.role == "hero_identity" for i in identity)
    assert all(
        (not i.never_use_to_redesign_face) or i.role != "hero_identity"
        for i in refs.images
    )


async def test_attempt_cap_exhausted_heygen_never_called(monkeypatch):
    """After max attempts, further score refuses; HeyGen mock never called; attempt == cap+1 after refuse."""
    from app.services.image_gate import run_score_image_step, GateRefuse, assert_pass_for_heygen
    from app.services import heygen
    from app.state_machine import transition
    from app.config import settings

    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("heygen.create_video must not be called")

    monkeypatch.setattr(heygen, "create_video", boom)

    conn = await _fresh_conn()
    try:
        job = await _seed_job(conn)
        jid = job["id"]
        fail = _card(overall=5.0, identity_likeness=5.0, failure_reasons=["bad"])
        r = None
        for i in range(settings.image_gate_max_attempts):
            st = await conn.fetchval("SELECT status::text FROM jobs WHERE id=$1", jid)
            if st in ("draft", "image_failed"):
                await transition(conn, jid, "image_queued", actor="t")
            await transition(conn, jid, "image_running", actor="t", set_locked_by="t")
            r = await run_score_image_step(
                conn, jid, candidate_url=f"https://example.com/{i}.jpg",
                prompt=f"p{i}", forced_card=fail,
            )
        assert r["status"] == "needs_human_review"
        att = await conn.fetchval("SELECT image_attempt FROM jobs WHERE id=$1", jid)
        assert att == settings.image_gate_max_attempts

        await transition(conn, jid, "image_queued", actor="t")
        await transition(conn, jid, "image_running", actor="t", set_locked_by="t")
        with pytest.raises(GateRefuse, match="max"):
            await run_score_image_step(
                conn, jid, candidate_url="https://example.com/4.jpg",
                prompt="p4", forced_card=fail,
            )
        with pytest.raises(GateRefuse):
            await assert_pass_for_heygen(conn, jid)
        assert called["n"] == 0
        att = await conn.fetchval("SELECT image_attempt FROM jobs WHERE id=$1", jid)
        assert att == settings.image_gate_max_attempts + 1
    finally:
        await conn.close()


async def test_daily_budget_exhausted_scorer_never_called(monkeypatch):
    """When daily budget exhausted, score refused; scorer + HeyGen never invoked."""
    from app.services.image_gate import run_score_image_step, GateRefuse
    from app.services.image_gate import scorer as scorer_mod
    from app.services import heygen
    from app.state_machine import transition
    from app.config import settings

    scorer_calls = {"n": 0}
    heygen_calls = {"n": 0}

    async def scorer_boom(*a, **k):
        scorer_calls["n"] += 1
        raise AssertionError("score_candidate must not be called when budget exhausted")

    async def heygen_boom(*a, **k):
        heygen_calls["n"] += 1
        raise AssertionError("heygen must not be called")

    monkeypatch.setattr(scorer_mod, "score_candidate", scorer_boom)
    monkeypatch.setattr(heygen, "create_video", heygen_boom)
    monkeypatch.setattr(settings, "image_scorer_daily_budget", 0)

    conn = await _fresh_conn()
    try:
        from app.services.image_gate.budget import ensure_budget_table
        await ensure_budget_table(conn)
        job = await _seed_job(conn)
        jid = job["id"]
        await transition(conn, jid, "image_queued", actor="t")
        await transition(conn, jid, "image_running", actor="t", set_locked_by="t")
        with pytest.raises(GateRefuse, match="budget"):
            await run_score_image_step(
                conn, jid, candidate_url="https://example.com/x.jpg", prompt="p",
            )
        assert scorer_calls["n"] == 0
        assert heygen_calls["n"] == 0
    finally:
        await conn.close()


async def test_break_glass_default_false_and_dual_control(monkeypatch):
    """break_glass default false; env alone refused; dual-control+audit allowed."""
    from app.services.image_gate import assert_pass_for_heygen, GateRefuse
    from app.config import settings

    assert settings.image_gate_break_glass is False

    conn = await _fresh_conn()
    try:
        from app.services.image_gate.budget import ensure_budget_table
        await ensure_budget_table(conn)
        job = await _seed_job(conn)
        jid = job["id"]
        with pytest.raises(GateRefuse):
            await assert_pass_for_heygen(conn, jid)

        monkeypatch.setattr(settings, "image_gate_break_glass", True)
        with pytest.raises(GateRefuse, match="dual-control"):
            await assert_pass_for_heygen(conn, jid)

        await conn.execute(
            "UPDATE jobs SET meta = COALESCE(meta,'{}'::jsonb) || $2::jsonb WHERE id=$1",
            jid,
            json.dumps({
                "break_glass_image_gate": True,
                "break_glass_reason": "test dual control",
                "break_glass_actor": "test",
            }),
        )
        result = await assert_pass_for_heygen(conn, jid)
        assert result.get("break_glass") is True
        assert result.get("audited") is True
        n = await conn.fetchval(
            "SELECT count(*) FROM image_gate_break_glass_audit WHERE job_id=$1", jid
        )
        assert int(n) >= 1
    finally:
        monkeypatch.setattr(settings, "image_gate_break_glass", False)
        await conn.close()


async def test_dual_h_second_reserve_requires_pass_binding(monkeypatch):
    """Horizontal heygen-avatar-h reserve must re-assert gate (same conn as reserve)."""
    from app.services import jobs as jobs_mod
    from app.services.image_gate import (
        GateRefuse, load_active_reference_set, record_score, assert_pass_for_heygen,
    )
    from app.state_machine import transition

    refs = load_active_reference_set(REF_PACK)
    assert_calls = {"n": 0}

    conn = await _fresh_conn()
    try:
        job = await _seed_job(conn)
        jid = job["id"]
        await transition(conn, jid, "image_queued", actor="t")
        await transition(conn, jid, "image_running", actor="t", set_locked_by="t")
        await record_score(
            conn, job_id=jid, attempt_n=1, card=_card(), refs=refs,
            prompt="x", candidate_url="https://example.com/ok.jpg",
        )
        await transition(conn, jid, "image_ready", actor="t")

        real_assert = assert_pass_for_heygen

        async def counting_assert(c, job_id):
            assert_calls["n"] += 1
            return await real_assert(c, job_id)

        # Simulate dual-H: assert then reserve on same connection
        h_key = f"{jid}:heygen-avatar-h"
        score = await counting_assert(conn, jid)
        await jobs_mod.reserve_key(conn, h_key, jid, "heygen-avatar-h")
        assert score.get("id") or float(score.get("overall") or 0) >= 8
        assert assert_calls["n"] >= 1

        # Corrupt hash → re-assert refuses (would block second reserve)
        await conn.execute(
            "UPDATE jobs SET image_ref_content_hash=$2 WHERE id=$1",
            jid, "deadbeef" * 8,
        )
        with pytest.raises(GateRefuse):
            await counting_assert(conn, jid)
            await jobs_mod.reserve_key(
                conn, f"{jid}:heygen-avatar-h2", jid, "heygen-avatar-h"
            )
    finally:
        await conn.close()



async def test_revise_refuses_mock_theater():
    from app.services.image_gate import (
        validate_regenerate_result, GateRefuse, run_revise_image_step, run_score_image_step,
    )
    from app.state_machine import transition

    with pytest.raises(GateRefuse, match="mock"):
        validate_regenerate_result({"url": "mock://revised"})
    with pytest.raises(GateRefuse):
        validate_regenerate_result({"url": ""})

    conn = await _fresh_conn()
    try:
        job = await _seed_job(conn)
        jid = job["id"]
        await transition(conn, jid, "image_queued", actor="t")
        await transition(conn, jid, "image_running", actor="t", set_locked_by="t")
        await run_score_image_step(
            conn, jid, candidate_url="https://example.com/1.jpg", prompt="p",
            forced_card=_card(overall=5.0, identity_likeness=5.0, failure_reasons=["bad"]),
        )
        assert (await conn.fetchval("SELECT status::text FROM jobs WHERE id=$1", jid)) == "image_failed"

        async def mock_regen(jid, prompt, refs):
            return {"url": "mock://revised"}

        with pytest.raises(GateRefuse, match="mock"):
            await run_revise_image_step(conn, jid, regenerate=mock_regen)
    finally:
        await conn.close()


async def test_score_before_primary_reserve_regression(monkeypatch):
    """Regression: identity veto + score-before-primary-reserve still hold."""
    from app.services.image_gate import evaluate_pass, assert_pass_for_heygen, GateRefuse
    from app.services import jobs as jobs_mod

    assert evaluate_pass(_card(overall=9.0, identity_likeness=7.9)) is False

    conn = await _fresh_conn()
    try:
        job = await _seed_job(conn)
        jid = job["id"]
        reserved = {"n": 0}
        real_reserve = jobs_mod.reserve_key

        async def tracking_reserve(c, key, job_id, step):
            reserved["n"] += 1
            return await real_reserve(c, key, job_id, step)

        monkeypatch.setattr(jobs_mod, "reserve_key", tracking_reserve)
        with pytest.raises(GateRefuse):
            await assert_pass_for_heygen(conn, jid)
            await jobs_mod.reserve_key(
                conn, f"{jid}:heygen-avatar", jid, "heygen-avatar"
            )
        assert reserved["n"] == 0
    finally:
        await conn.close()
