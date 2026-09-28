"""PR #22 re-audit residuals.

PR22-F1: verified approval_token never published / persisted / echoed.
PR22-F2: approvals are single-use (durable spend; concurrent replay => one success).
PR22-F4/F5: transcript-turn meta never persists audio; migration 009 scrub.
PR21-F6/PR22-F6: durable publish idempotency (retry returns prior message_id).
"""
from __future__ import annotations

import asyncio
import base64
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from mcp_servers import job_command_campaign_store as store_mod
from mcp_servers import job_command_pipeline_policy as policy
from mcp_servers.job_command_pipeline_policy import JobCommandEvent, mint_approval_token
from tests.test_pr21_campaign_audit_fixes import (  # noqa: F401  (fixtures)
    APPROVAL_SECRET, _FakePublisher, _event, _post, _render,
    allowlist, campaign_db, ingress, secrets,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_009 = ROOT / "supabase" / "migrations" / "20260317000009_job_command_single_use_approvals.sql"
AUDIO_B64 = base64.b64encode((b"ID3TURN-AUDIO-MARKER" + bytes(range(256))) * 8).decode()


def _published_json(publisher) -> list[dict]:
    return [json.loads(body) for _, body, _ in publisher.calls]


async def _db_text(dsn: str, tables: tuple[str, ...]) -> str:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        chunks = []
        for table in tables:
            chunks += [r["j"] for r in await conn.fetch(f"SELECT row_to_json(t)::text AS j FROM {table} t")]
        return "\n".join(chunks)
    finally:
        await conn.close()


# ======================================================== PR22-F1 ==========

def test_pr22_f1_canonical_payload_and_idempotency_exclude_token(allowlist, secrets):
    event = JobCommandEvent.model_validate(_render())
    token = event.approval_token
    body = policy.canonical_event_payload(event)
    assert token and token.encode() not in body
    assert "approval_token" not in json.loads(body)
    assert json.loads(body)["approval_ref"] == "appr-001"
    assert policy.idempotency_key(event) == sha256(body).hexdigest()
    assert token not in repr(event)
    assert "approval_token" not in policy.public_event_dict(event)


def test_pr22_f1_validation_errors_never_echo_token(allowlist, secrets):
    from pydantic import ValidationError

    payload = _render()
    payload["page_url"] = "https://evil.example/how-it-works"
    with pytest.raises(ValidationError) as exc:
        JobCommandEvent.model_validate(payload)
    detail = policy.validation_error_detail(exc.value)
    assert "host is not on" in detail
    assert payload["approval_token"] not in detail


def test_pr22_f1_mcp_validate_response_has_no_token(allowlist, secrets):
    from mcp_servers.vertex_job_command_mcp import validate_campaign_event

    payload = _render()
    result = validate_campaign_event(payload)
    assert payload["approval_token"] not in json.dumps(result)


@pytest.mark.postgres
def test_pr22_f1_ingress_publish_and_store_never_contain_token(ingress, campaign_db):
    client, publisher = ingress
    payload = _render()
    token = payload["approval_token"]
    response = _post(client, payload)
    assert response.status_code == 200, response.text
    _, body, attrs = publisher.calls[0]
    assert token.encode() not in body
    assert token not in json.dumps(attrs)
    assert token not in response.text
    stored = asyncio.run(_db_text(campaign_db, ("job_command_publish_log", "job_command_spent_approvals")))
    assert token not in stored
    assert sha256(token.encode()).hexdigest() in stored  # only the digest is kept


@pytest.mark.postgres
async def test_pr22_f1_mcp_publish_body_has_no_token(allowlist, secrets, campaign_db, monkeypatch):
    from mcp_servers import vertex_job_command_mcp as mcp_mod

    fake = _FakePublisher()

    class _Client:
        def topic_path(self, project, topic):
            return f"projects/{project}/topics/{topic}"

        def publish(self, topic, data, **attrs):
            return fake.publish(topic, data, **attrs)

    monkeypatch.setattr(mcp_mod.pubsub_v1, "PublisherClient", _Client)
    monkeypatch.setenv("JOB_COMMAND_GCP_PROJECT", "jc-test")
    payload = _render()
    result = await mcp_mod.publish_campaign_event(payload)
    assert result["message_id"] == "msg-1"
    assert payload["approval_token"].encode() not in fake.calls[0][1]
    assert payload["approval_token"] not in json.dumps(result)
    # MCP replay of the same approval with a new event is refused, not published.
    replay = dict(payload, event_id=str(uuid4()))
    again = await mcp_mod.publish_campaign_event(replay)
    assert again["published"] is False and again["status"] == "pending_approval"
    assert len(fake.calls) == 1


# ======================================================== PR22-F2 ==========

@pytest.mark.postgres
def test_pr22_f2_approval_replay_with_new_event_is_409(ingress):
    client, publisher = ingress
    payload = _render()
    assert _post(client, payload).status_code == 200
    replay = dict(payload, event_id=str(uuid4()))
    response = _post(client, replay)
    assert response.status_code == 409
    assert response.json()["status"] == "pending_approval"
    assert response.json()["reason"] == "approval_spent"
    assert len(publisher.calls) == 1


@pytest.mark.postgres
def test_pr22_f2_reminted_token_for_same_approval_id_is_still_spent(ingress):
    import time

    client, publisher = ingress
    first = _render()
    assert _post(client, first).status_code == 200
    reminted = mint_approval_token(approval_id="appr-001", campaign_id=first["campaign_id"],
                                   approved_by="operator@jobcommand", secret=APPROVAL_SECRET,
                                   now=int(time.time()) - 5)
    second = dict(first, event_id=str(uuid4()), approval_token=reminted)
    assert second["approval_token"] != first["approval_token"]
    assert _post(client, second).status_code == 409
    assert len(publisher.calls) == 1


@pytest.mark.postgres
def test_pr22_f2_retry_of_same_event_is_not_a_replay(ingress):
    client, publisher = ingress
    payload = _render()
    first = _post(client, payload)
    retry = _post(client, payload)
    assert first.status_code == retry.status_code == 200
    assert retry.json()["duplicate"] is True
    assert retry.json()["message_id"] == first.json()["message_id"]
    assert len(publisher.calls) == 1


@pytest.mark.postgres
def test_pr22_f2_store_not_configured_fails_closed(ingress, monkeypatch):
    client, publisher = ingress
    monkeypatch.delenv("JOB_COMMAND_CAMPAIGN_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://should-not-be-used")  # no MSE fallback
    assert _post(client, _render()).status_code == 503
    assert _post(client, _event()).status_code == 503
    assert publisher.calls == []


@pytest.mark.postgres
async def test_pr22_f2_concurrent_replay_yields_exactly_one_success(allowlist, secrets, campaign_db):
    base = _render()
    events = [JobCommandEvent.model_validate(dict(base, event_id=str(uuid4()))) for _ in range(8)]
    published: list[bytes] = []

    def publish_fn(payload, attributes):
        published.append(payload)
        return f"msg-{len(published)}"

    store = store_mod.store_from_env()
    results = await asyncio.gather(
        *[store_mod.guarded_publish(e, publish_fn=publish_fn, attributes={}, store=store) for e in events],
        return_exceptions=True,
    )
    ok = [r for r in results if isinstance(r, dict)]
    replays = [r for r in results if isinstance(r, store_mod.ApprovalReplay)]
    assert len(ok) == 1, results
    assert len(replays) == 7, results
    assert len(published) == 1

    import asyncpg
    conn = await asyncpg.connect(campaign_db)
    try:
        assert await conn.fetchval("SELECT count(*) FROM job_command_spent_approvals") == 1
        assert await conn.fetchval("SELECT count(*) FROM job_command_publish_log") == 1  # replays rolled back
        assert await conn.fetchval("SELECT status FROM job_command_publish_log") == "published"
    finally:
        await conn.close()


@pytest.mark.postgres
async def test_pr22_f2_failed_publish_can_be_retried_by_same_event_only(allowlist, secrets, campaign_db):
    event = JobCommandEvent.model_validate(_render())
    store = store_mod.store_from_env()

    def boom(payload, attributes):
        raise RuntimeError("pubsub down")

    with pytest.raises(RuntimeError):
        await store_mod.guarded_publish(event, publish_fn=boom, attributes={}, store=store)
    other = JobCommandEvent.model_validate(dict(_render(campaign_id=str(event.campaign_id)),
                                                event_id=str(uuid4())))
    with pytest.raises(store_mod.ApprovalReplay):
        await store_mod.guarded_publish(other, publish_fn=lambda p, a: "x", attributes={}, store=store)
    result = await store_mod.guarded_publish(event, publish_fn=lambda p, a: "msg-ok", attributes={}, store=store)
    assert result["message_id"] == "msg-ok" and result["duplicate"] is False


# ================================================ PR21-F6 / PR22-F6 ========

@pytest.mark.postgres
def test_pr22_f6_duplicate_event_returns_prior_message_id(ingress):
    client, publisher = ingress
    payload = _event()
    first = _post(client, payload)
    second = _post(client, payload)
    assert first.status_code == second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["message_id"] == first.json()["message_id"]
    assert len(publisher.calls) == 1


@pytest.mark.postgres
async def test_pr22_f6_concurrent_duplicates_publish_once(allowlist, secrets, campaign_db):
    event = JobCommandEvent.model_validate(_event())
    calls = []

    def publish_fn(payload, attributes):
        calls.append(payload)
        return "msg-1"

    store = store_mod.store_from_env()
    results = await asyncio.gather(
        *[store_mod.guarded_publish(event, publish_fn=publish_fn, attributes={}, store=store) for _ in range(6)],
        return_exceptions=True,
    )
    assert len(calls) == 1
    for r in results:
        assert isinstance(r, (dict, store_mod.PublishInProgress)), r
        if isinstance(r, dict):
            assert r["message_id"] == "msg-1"


@pytest.mark.postgres
async def test_pr22_f6_stale_pending_claim_is_reclaimed(allowlist, secrets, campaign_db):
    import asyncpg

    event = JobCommandEvent.model_validate(_event())
    key = policy.idempotency_key(event)
    conn = await asyncpg.connect(campaign_db)
    try:
        await conn.execute(
            """INSERT INTO job_command_publish_log(idempotency_key, event_id, event_type, campaign_id, updated_at)
               VALUES($1,$2,$3,$4, now() - interval '1 hour')""",
            key, event.event_id, event.event_type, event.campaign_id,
        )
    finally:
        await conn.close()
    store = store_mod.store_from_env()
    result = await store_mod.guarded_publish(event, publish_fn=lambda p, a: "msg-2", attributes={}, store=store)
    assert result["message_id"] == "msg-2"


# ================================================= PR22-F4 / F5 ============

def _transcript_event(session_id: str | None) -> dict:
    data = {
        "agent_id": "agent_private_123",
        "conversation_id": "conv_turn_audio",
        "transcript": [
            {"role": "user", "message": "hello there", "audio": AUDIO_B64, "raw_audio": AUDIO_B64,
             "extra": {"nested_blob": AUDIO_B64}, "time_in_call_secs": 1},
            {"role": "agent", "message": AUDIO_B64},
        ],
    }
    if session_id:
        data["conversation_initiation_client_data"] = {"dynamic_variables": {"job_command_session_id": session_id}}
    return {"type": "post_call_transcription", "event_timestamp": 1739537500, "data": data}


def test_pr22_f4_turn_meta_drops_audio_keys_and_blobs():
    from app.services import voice_sessions

    turns = voice_sessions._turns(_transcript_event(str(uuid4())))
    text = json.dumps(turns)
    assert AUDIO_B64[:64] not in text
    assert "audio" not in turns[0]["meta"] and "raw_audio" not in turns[0]["meta"]
    assert turns[0]["meta"]["time_in_call_secs"] == 1
    assert turns[1]["text"] == "[redacted binary content]"


@pytest.fixture
async def voice_pg(require_database_url, monkeypatch, tmp_path):
    import asyncpg
    from app import db as dbmod
    from app.config import settings
    from app.services import voice_sessions
    from datetime import datetime, timedelta, timezone
    from tests.conftest import apply_schema, truncate_app_tables

    await dbmod.close()
    pool = await asyncpg.create_pool(require_database_url, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await apply_schema(conn)
        await truncate_app_tables(conn)
    dbmod._pool = pool
    for name, value in {
        "job_command_operator_authorized": True, "elevenlabs_agent_id": "agent_private_123",
        "job_command_supported_languages": "en", "job_command_default_language": "en",
        "asset_storage_dir": str(tmp_path / "assets"),
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(voice_sessions.elevenlabs_agents, "create_signed_url", AsyncMock(return_value={
        "signed_url": "wss://api.elevenlabs.test/c?token=o", "agent_id": "agent_private_123",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15)}))
    yield pool
    await pool.close()
    dbmod._pool = None


VOICE_TABLES = ("voice_session_events", "voice_sessions", "voice_transcript_turns",
                "voice_transcription_runs", "voice_work_queue")


@pytest.mark.postgres
async def test_pr22_f4_transcript_turn_audio_never_persists(voice_pg, require_database_url):
    from app.services import voice_sessions

    issued = await voice_sessions.create_session(visitor_ref="u1", requested_language="en",
                                                 surface="web", archive_consent=False)
    session_id = issued["session"]["id"]
    await voice_sessions.ingest_postcall_event(_transcript_event(session_id))
    everything = await _db_text(require_database_url, VOICE_TABLES)
    assert AUDIO_B64[:64] not in everything
    async with voice_pg.acquire() as conn:
        meta = json.loads(await conn.fetchval(
            "SELECT meta FROM voice_transcript_turns WHERE session_id=$1 AND ordinal=0", session_id))
    assert "audio" not in meta and "raw_audio" not in meta


@pytest.mark.postgres
async def test_pr22_f5_migration_009_scrubs_all_audio_columns(voice_pg, require_database_url):
    from app.services import voice_sessions

    issued = await voice_sessions.create_session(visitor_ref="u1", requested_language="en",
                                                 surface="web", archive_consent=True)
    sid = issued["session"]["id"]
    matched = {"type": "post_call_audio", "data": {"conversation_id": "c1", "audio_base64": AUDIO_B64,
               "nested": [{"recording": AUDIO_B64}, {"blob": AUDIO_B64}], "note": "keep me"}}
    unmatched = {"type": "post_call_transcription", "event_timestamp": 5,
                 "data": {"conversation_id": "c2", "agent_id": "a", "audio": AUDIO_B64,
                          "analysis": {"summary": "private"}, "transcript": [{"message": "hi"}]}}
    async with voice_pg.acquire() as conn:
        await conn.execute(
            "INSERT INTO voice_session_events(session_id, provider_event_key, provider_event_type, payload) "
            "VALUES($1,'m1','post_call_audio',$2::jsonb), (NULL,'u1','post_call_transcription',$3::jsonb)",
            sid, json.dumps(matched), json.dumps(unmatched))
        await conn.execute(
            "INSERT INTO voice_transcript_turns(session_id, source, ordinal, speaker_role, text, meta) "
            "VALUES($1,'elevenlabs',0,'user','hello',$2::jsonb), ($1,'elevenlabs',1,'agent',$3,'{}'::jsonb)",
            sid, json.dumps({"audio": AUDIO_B64, "extra": {"raw": AUDIO_B64}, "time_in_call_secs": 3}), AUDIO_B64)
        await conn.execute("UPDATE voice_work_queue SET payload = $1::jsonb", json.dumps({"audio_data": AUDIO_B64}))
        sql = MIGRATION_009.read_text()
        await conn.execute(sql)
        await conn.execute(sql)  # idempotent
        m = json.loads(await conn.fetchval("SELECT payload FROM voice_session_events WHERE provider_event_key='m1'"))
        u = json.loads(await conn.fetchval("SELECT payload FROM voice_session_events WHERE provider_event_key='u1'"))
        turns = await conn.fetch("SELECT ordinal, text, meta FROM voice_transcript_turns ORDER BY ordinal")
    everything = await _db_text(require_database_url, VOICE_TABLES)
    assert AUDIO_B64[:64] not in everything
    # SQL redaction summary matches the runtime Python summary exactly.
    assert m["data"]["audio_base64"] == voice_sessions._audio_summary(AUDIO_B64)
    assert m["data"]["note"] == "keep me"
    assert u == {"type": "post_call_transcription", "event_timestamp": 5,
                 "data": {"conversation_id": "c2", "agent_id": "a"},
                 "minimal_envelope": True, "audio_redacted": True}
    meta0 = json.loads(turns[0]["meta"])
    assert "audio" not in meta0 and meta0["time_in_call_secs"] == 3
    assert turns[1]["text"] == "[redacted binary content]"
